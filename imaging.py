#!/usr/bin/env python

import os
import sys
import csv
import subprocess
import numpy
import lofar.parameterset

from multiprocessing import cpu_count
from multiprocessing.pool import ThreadPool

from tempfile import mkstemp, mkdtemp
from contextlib import contextmanager

from pyrap.tables import table

# All temporary writes go to scratch space on the node.
scratch = os.getenv("TMPDIR")

def patch_parset(parset_filename, data, output_dir=None):
    temp_parset = lofar.parameterset.parameterset(parset_filename)
    for key, value in data.iteritems():
        temp_parset.replace(key, str(value))
    fd, output = mkstemp(dir=output_dir)
    temp_parset.writeFile(output)
    os.close(fd)
    return output


@contextmanager
def patched_parset(parset_filename, data, output_dir=None, unlink=True):
    filename = patch_parset(parset_filename, data, output_dir)
    try:
        yield filename
    finally:
        if unlink: os.unlink(filename)


def run_process(executable, *args):
    args = list(args)
    args.insert(0, executable)
    subprocess.check_call(args)


def run_awimager(parset_filename, parset_keys):
    with patched_parset(parset_filename, parset_keys, unlink=False) as parset:
        run_process("awimager", parset)
    return parset_keys["image"]


def run_ndppp(parset_filename, parset_keys):
    with patched_parset(parset_filename, parset_keys) as parset:
        run_process("NDPPP", parset)
    return parset_keys["msout"]


def run_calibrate_standalone(parset_filename, input_ms, skymodel):
    return run_process("calibrate-stand-alone", input_ms, parset_filename, skymodel)


def find_bad_stations(msname):
    # Using scripts developed by Martinez & Pandey
    statsdir = os.path.join(scratch, "stats")
    run_process("asciistats.py", "-i", msname, "-r", statsdir)
    statsfile = os.path.join(statsdir, os.path.basename(msname) + ".stats")
    run_process("statsplot.py", "-i", statsfile, "-o", os.path.join(scratch, "stats"))
    bad_stations = []
    with open(os.path.join(scratch, "stats.tab"), "r") as f:
        for line in f:
            if line.strip()[0] == "#": continue
            if line.split()[-1] == "True":
                bad_stations.append(line.split()[1])
    return bad_stations


def strip_stations(msin, msout, stationlist):
    t = table(msin)
    output = t.query("""
        all(
            [ANTENNA1, ANTENNA2] not in
            [
                select rowid() from ::ANTENNA where NAME in %s
            ]
        )
        """ % str(stationlist)
    )
    # Is a deep copy really necessary here?
    output.copy(msout, deep=True)


def estimate_noise(msin, cellsize, robust, maxbl, wmax, wplanes):
    noise_image = mkdtemp(dir=scratch)

    # Default parameters -- hardcoded in Antonia's script
    nchan = 10
    niter = 0
    npix = 256
    operation = "image"
    stokes = "IQUV"

    run_process(
        "awimager",
        "cellsize=%s" % (cellsize,),
        "data=CORRECTED_DATA",
        "ms=%s" % (msin,),
        "nchan=%d" % (nchan,),
        "niter=%d" % (niter,),
        "npix=%d" % (npix,),
        "operation=%s" % (operation,),
        "robust=%d" % (robust,),
        "select=\"sumsqr(UVW[:2])<%.1e\"" % (maxbl**2,),
        "stokes=%s" % (stokes,),
        "wmax=%f" % (wmax,),
        "wprojplanes=%d" % (wplanes,),
        "image=%s" % (noise_image,)
    )

    t = table(noise_image)
    box_size = 25
    # Why is there a 3 in here? Are we calculating the noise in Stokes V?
    # (this is lifted from Antonia, which is lifted from George, ...!)
    noise = t.getcol('map')[0, 0, 3, npix/2-box_size:npix/2+box_size, npix/2-box_size:npix/2+box_size].std()
    t.close()
    return noise


def make_mask(msin, skymodel, cellsize, npix):
    mask_image = mkdtemp(dir=scratch)
    mask_sourcedb = mkdtemp(dir=scratch)
    operation = "empty"
    run_process(
        "awimager",
        "cellsize=%s" % (cellsize,),
        "ms=%s" % (msin,),
        "npix=%d" % (npix,),
        "operation=%s" % (operation,),
        "image=%s" % (mask_image,),
        "nchan=10",
        "niter=0",
        "robust=0",
        "select=\"sumsqr(UVW[:2])<%.1e\"" % (3000**2,),
        "stokes=I",
        "wprojplanes=129",
        "wmax=3000",
    )
    run_process(
        "makesourcedb",
        "in=%s" % (skymodel,),
        "out=%s" % (mask_sourcedb,),
        "format=<"
    )
    run_process(
        "python",
        "/home/jswinban/pipeline/msss_mask.py",
        mask_image,
        mask_sourcedb
    )
    return mask_image


def get_parset_subset(parset, prefix):
    subset = parset.makeSubset(prefix + ".", "")
    fd, parset_name = mkstemp(dir=scratch)
    subset.writeFile(parset_name)
    return parset_name


if __name__ == "__main__":
    # We take the name of a parset containing the data files to process as our
    # command line argument.
    input_parset = lofar.parameterset.parameterset(sys.argv[1])
    msin = input_parset.getStringVector("msin")

    # We'll run as many simultaneous jobs as we have CPUs
    # (Except for imaging?)
    pool = ThreadPool(cpu_count())

    # Initial NDPPP run to flag & compress the visibilities
    ndppp_parset = get_parset_subset(input_parset, "ndppp")
    def first_ndppp(ms):
        return run_ndppp(ndppp_parset,
            {
                "msin": ms,
                "msout": os.path.join(scratch, os.path.basename(ms)),
                "aoflagger.strategy": os.path.join(os.getenv("LOFARROOT"), "share/rfistrategies/LBAdefault")
            }
        )
    datafiles = pool.map(first_ndppp, msin)

    bbs_parset = get_parset_subset(input_parset, "bbs")
    skymodel = input_parset.getString("skymodel")
    def calibrate_standalone(ms):
        run_calibrate_standalone(bbs_parset, ms, skymodel)
        return ms
    datafiles = pool.map(calibrate_standalone, datafiles)

    combined_ms = os.path.join(scratch, "combined.MS")
    run_ndppp(get_parset_subset(input_parset, "combine"),
        {
            "msin": str(datafiles),
            "msout": combined_ms
        }
    )

    bad_stations = find_bad_stations(combined_ms)
    stripped_ms = os.path.join(scratch, "stripped.MS")
    strip_stations(combined_ms, stripped_ms, bad_stations)

    maxbl = input_parset.getFloat("maxbl")
    cellsize = input_parset.getString("awimager.cellsize")
    robust = input_parset.getFloat("awimager.robust")
    wmax = input_parset.getFloat("awimager.wmax")
    wplanes = input_parset.getInt("awimager.wprojplanes")
    npix = input_parset.getFloat("awimager.npix")

    threshold = input_parset.getFloat("noise_multiplier") * estimate_noise(stripped_ms, cellsize, robust, maxbl, wmax, wplanes)
    mask = make_mask(stripped_ms, skymodel, cellsize, npix)

    aw_parset_name = get_parset_subset(input_parset, "awimager")
    print run_awimager(aw_parset_name,
        {
            "ms": stripped_ms,
            "mask": mask,
            "threshold": "%fJy" % (threshold,),
            "select": "\"sumsqr(UVW[:2])<%.1e\"" % (maxbl**2,),
            "image": input_parset.getString("output")
        }
    )
