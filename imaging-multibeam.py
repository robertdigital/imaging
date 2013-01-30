#!/usr/bin/env python

import os
import sys
import numpy
import math
import glob
import errno
import lofar.parameterset

from multiprocessing import cpu_count
from multiprocessing.pool import ThreadPool

from shutil import copytree
from itertools import chain
from tempfile import mkstemp, mkdtemp

from pyrap.tables import table

from utility import patched_parset
from utility import run_process
from utility import time_code

# All temporary writes go to scratch space on the node.
scratch = os.getenv("TMPDIR")

def run_awimager(parset_filename, parset_keys, initscript=None):
    with patched_parset(parset_filename, parset_keys) as parset:
        run_process("awimager", parset, initscript=initscript)
    return parset_keys["image"]


def run_ndppp(parset_filename, parset_keys, initscript=None):
    with patched_parset(parset_filename, parset_keys) as parset:
        run_process("NDPPP", parset, initscript=initscript)
    return parset_keys["msout"]


def run_calibrate_standalone(parset_filename, input_ms, skymodel, initscript=None):
    run_process("calibrate-stand-alone", input_ms, parset_filename, skymodel, initscript=initscript)
    return input_ms


def find_bad_stations(msname, initscript=None):
    # Using scripts developed by Martinez & Pandey
    statsdir = os.path.join(scratch, "stats")
    run_process("asciistats.py", "-i", msname, "-r", statsdir, initscript=initscript)
    statsfile = os.path.join(statsdir, os.path.basename(msname) + ".stats")
    run_process("statsplot.py", "-i", statsfile, "-o", os.path.join(scratch, "stats"), initscript=initscript)
    bad_stations = []
    with open(os.path.join(scratch, "stats.tab"), "r") as f:
        for line in f:
            if line.strip()[0] == "#": continue
            if line.split()[-1] == "True":
                bad_stations.append(line.split()[1])
    return bad_stations


def strip_stations(msin, msout, stationlist):
    t = table(msin)
    if stationlist:
        output = t.query("""
            all(
                [ANTENNA1, ANTENNA2] not in
                [
                    select rowid() from ::ANTENNA where NAME in %s
                ]
            )
            """ % str(stationlist)
        )
    else:
        output = t
    # Is a deep copy really necessary here?
    output.copy(msout, deep=True)


def limit_baselines(msin, msout, maxbl):
    t = table(msin)
    out = t.query("sumsqr(UVW[:2])<%.1e" % (maxbl**2,))
    out.copy(msout, deep=False)


def estimate_noise(msin, parset, wmax, box_size, awim_init=None):
    noise_image = mkdtemp(dir=scratch)

    run_awimager(parset,
        {
            "ms": msin,
            "image": noise_image,
            "wmax": wmax
        },
        initscript=awim_init
    )

    t = table(noise_image)
    parset = lofar.parameterset.parameterset(parset)
    npix = parset.getFloat("npix")
    # Why is there a 3 in here? Are we calculating the noise in Stokes V?
    # (this is lifted from Antonia, which is lifted from George, ...!)
    noise = t.getcol('map')[0, 0, 3, npix/2-box_size:npix/2+box_size, npix/2-box_size:npix/2+box_size].std()
    t.close()
    return noise


def make_mask(msin, parset, skymodel, executable, awim_init=None):
    mask_image = mkdtemp(dir=scratch)
    mask_sourcedb = mkdtemp(dir=scratch)
    operation = "empty"

    awimager_parset = lofar.parameterset.parameterset(parset)
    cellsize = awimager_parset.getString("cellsize")
    npix = awimager_parset.getFloat("npix")
    stokes = awimager_parset.getString("stokes")

    run_process(
        "awimager",
        "cellsize=%s" % (cellsize,),
        "ms=%s" % (msin,),
        "npix=%d" % (npix,),
        "operation=%s" % (operation,),
        "image=%s" % (mask_image,),
        "stokes=%s" % (stokes,),
        initscript=awim_init
    )
    run_process(
        "makesourcedb",
        "in=%s" % (skymodel,),
        "out=%s" % (mask_sourcedb,),
        "format=<"
    )
    run_process(
        executable,
        mask_image,
        mask_sourcedb
    )
    return mask_image


def get_parset_subset(parset, prefix):
    subset = parset.makeSubset(prefix + ".", "")
    fd, parset_name = mkstemp(dir=scratch)
    subset.writeFile(parset_name)
    return parset_name

def get_file_list(root_dir, obsid, beam):
    return sorted(
        glob.glob(os.path.join(
            root_dir, obsid, "%s_SAP00%d_SB*_uv.MS.dppp" % (obsid, beam)
        ))
    )

def make_directory(path):
    try:
        os.makedirs(path)
    except OSError, e:
        if e.errno != errno.EEXIST:
            raise

def copy_to_work_area(input_file_list, work_area):
    outputs = []
    for ms_name in input_file_list:
        output_name = os.path.join(work_area, os.path.basename(ms_name))
        copytree(ms_name, output_name)
        outputs.append(output_name)
    return outputs


if __name__ == "__main__":
    # Our single command line argument is a parset containing all
    # configuration information we'll need.
    input_parset = lofar.parameterset.parameterset(sys.argv[1])

    # Change to appropriate working directory for logs, etc.
    #os.chdir(input_parset.getString("working_dir"))

    print "Locating input data and checking paths"
    ms_cal = {}
    ms_cal["datafiles"] = get_file_list(
        input_parset.getString("input_dir"),
        input_parset.getString("cal_obsid"),
        0
    )

    ms_cal["output_dir"] = os.path.join(
        input_parset.getString("output_dir"),
        "calibrator",
        input_parset.getString("cal_obsid")
    )
    make_directory(ms_cal["output_dir"])

    ms_target = {}
    for beam in range(input_parset.getInt("n_beams")):
        target_info = {}
        target_info['datafiles'] = get_file_list(
            input_parset.getString("input_dir"),
            input_parset.getString("target_obsid"),
            beam
        )
        assert(len(target_info['datafiles']) == len(ms_cal['datafiles']))
        target_info['output_dir'] = os.path.join(
            input_parset.getString("output_dir"),
            "target",
            input_parset.getString("target_obsid"),
            "SAP00%d" % (beam,)
        )
        make_directory(target_info["output_dir"])
        target_info["output_ms"] = os.path.join(target_info["output_dir"], "%s.MS" % (input_parset.getString("target_obsid"),))
        assert(not os.path.exists(target_info["output_ms"]))
        target_info["output_im"] = os.path.join(target_info["output_dir"], "%s.img" % (input_parset.getString("target_obsid"),))
        assert(not os.path.exists(target_info["output_im"]))
        pointing = map(math.degrees, table("%s::FIELD" % target_info["datafiles"][0]).getcol("REFERENCE_DIR")[0][0])
        target_info["skymodel"] = os.path.join(
            input_parset.getString("skymodel_dir"),
            "%.2f_%.2f.skymodel" % (pointing[0], pointing[1])
        )
        assert(os.path.exists(target_info["skymodel"]))
        ms_target[beam] = target_info

    # Copy to working directories
    print "Copying calibrator subbands to output"
    ms_cal["datafiles"] = copy_to_work_area(ms_cal["datafiles"], ms_cal["output_dir"])
    for beam in ms_target.iterkeys():
        print "Copying beam %d to scratch area" % (beam,)
        ms_target[beam]["datafiles"] = copy_to_work_area(
            ms_target[beam]["datafiles"], scratch
        )

    # We'll run as many simultaneous jobs as we have CPUs
    pool = ThreadPool(cpu_count())

    # Calibration of each calibrator subband
    os.chdir(ms_cal['output_dir']) # Logs will get dumped here
    calcal_parset = get_parset_subset(input_parset, "calcal.parset")
    def calibrate_calibrator(cal):
        source = table("%s::OBSERVATION" % (cal,)).getcol("LOFAR_TARGET")['array'][0].lower().replace(' ', '')
        skymodel = os.path.join(
            input_parset.getString("skymodel_dir"),
            "%s.skymodel" % (source,)
        )
        print "Calibrating %s with skymodel %s" % (cal, skymodel)
        run_calibrate_standalone(calcal_parset, cal, skymodel)
    with time_code("Calibration of calibrator"):
        pool.map(calibrate_calibrator, ms_cal["datafiles"])

    # Clip calibrator parmdbs
    def clip_parmdb(sb):
        run_process(
            input_parset.getString("pdbclip.executable"),
            "--auto",
            "--sigma=%f" % (input_parset.getFloat("pdbclip.sigma"),),
            os.path.join(sb, "instrument")
        )
    with time_code("Clip calibrator instrument databases"):
        pool.map(lambda sb: clip_parmdb(sb), ms_cal["datafiles"])

    # Transfer calibration solutions to targets
    transfer_parset = get_parset_subset(input_parset, "transfer.parset")
    transfer_skymodel = input_parset.getString("transfer.skymodel")
    def transfer_calibration(ms_pair):
        cal, target = ms_pair
        print "Transferring solution from %s to %s" % (cal, target)
        parmdb_name = mkdtemp(dir=scratch)
        run_process("parmexportcal", "in=%s/instrument/" % (cal,), "out=%s" % (parmdb_name,))
        run_process("calibrate-stand-alone", "--parmdb", parmdb_name, target, transfer_parset, transfer_skymodel)
    with time_code("Transfer of calibration solutions"):
        for target in ms_target.itervalues():
            pool.map(transfer_calibration, zip(ms_cal["datafiles"], target["datafiles"]))

    # Combine with NDPPP
    def combine_ms(target_info):
        output = os.path.join(scratch, mkdtemp(dir=scratch), "combined.MS")
        run_ndppp(
            get_parset_subset(input_parset, "combine.parset"),
            {
                "msin": str(target_info["datafiles"]),
                "msout": output
            }
        )
        target_info["combined"] = output
    with time_code("Combining target subbands"):
        pool.map(combine_ms, ms_target.values())


    # Phase only calibration of combined target subbands
    target_skymodel = input_parset.getString("phaseonly.skymodel")
    print "Running phase only calibration"
    def phaseonly(target_info):
        os.chdir(target_info['output_dir']) # Logs will get dumped here
        run_calibrate_standalone(
            get_parset_subset(input_parset, "phaseonly.parset"),
            target_info["combined"],
            target_info["skymodel"]
        )
    with time_code("Phase-only calibration"):
        pool.map(phaseonly, ms_target.values())

    # Strip bad stations.
    # Note that the combined, calibrated, stripped MS is one of our output
    # data products, so we save that with the name specified in the parset.
    def strip_bad_stations(target_info):
        bad_stations = find_bad_stations(target_info["combined_ms"])
        strip_stations(target_info["combined_ms"], target_info["output_ms"], bad_stations)
    with time_code("Strip bad stations"):
        pool.map(strip_bad_stations, ms_target.values())

    # Limit the length of the baselines we're using.
    # We'll image a reference table using only the short baselines.
    maxbl = input_parset.getFloat("limit.max_baseline")
    def limit_bl(target_info):
        target_info["bl_limit_ms"] = mkdtemp(dir=scratch)
        limit_baselines(target_info["output_ms"], target_info["bl_limit_ms"], maxbl)
    with time_code("Limiting maximum baseline length"):
        pool.map(limit_bl, ms_target.values())

    # We source a special build for using the "new" awimager
    awim_init = input_parset.getString("awimager.initscript")

    # Calculate the threshold for cleaning based on the noise in a dirty map
    # We don't use our threadpool here, since awimager is parallelized
    noise_parset_name = get_parset_subset(input_parset, "noise.parset")
    with time_code("Calculating threshold for cleaning"):
        for target_info in ms_target.values():
            print "Getting threshold for %s" % target_info["output_ms"]
            target_info["threshold"] = input_parset.getFloat("noise.multiplier") * estimate_noise(
                target_info["bl_limit_ms"],
                noise_parset_name,
                maxbl,
                input_parset.getFloat("noise.box_size")
            )

    # Make a mask for cleaning
    aw_parset_name = get_parset_subset(input_parset, "image.parset")
    with time_code("Making mask"):
        for target_info in ms_target.values():
            print "Making mask for %s" % target_info["output_ms"]
            target_info["mask"] = make_mask(
                target_info["bl_limit_ms"],
                aw_parset_name,
                target_info["skymodel"],
                input_parset.getString("make_mask.executable"),
                awim_init=awim_init
            )

    with time_code("Making images"):
        for target_info in ms_target.values():
            print "Making image %s" % target_info["output_im"]
            print run_awimager(aw_parset_name,
                {
                    "ms": target_info["bl_limit_ms"],
                    "mask": target_info["mask"],
                    "threshold": "%fJy" % (threshold,),
                    "image": target_info["output_im"],
                    "wmax": maxbl
                },
                initscript=awim_init
            )