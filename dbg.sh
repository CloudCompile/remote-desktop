#!/bin/bash
python3 dbg2.py 2>&1 | tail -30
rm -f dbg2.py dbg.sh dbg_server.py
