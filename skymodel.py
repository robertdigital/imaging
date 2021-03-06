import os
import sys
import glob
import math
from pyrap.tables import table

# One from each beam
important_subbands = ["000", "040", "080", "120", "160", "200"]
positions = []

for arg in sys.argv[1:]:
    for sb in important_subbands:
        for msname in glob.glob(os.path.join(arg, "L*SB%s_uv.MS.dppp" % (sb,))):
            t = table("%s::FIELD" % (msname,))
            positions.append(map(math.degrees, t.getcol("REFERENCE_DIR")[0][0]))

#print positions
for posn in positions:
    print "gsm.py %.2f_%.2f.skymodel %f %f 5 0.1" % (posn[0], posn[1], posn[0], posn[1])
