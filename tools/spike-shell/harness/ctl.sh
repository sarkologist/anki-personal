#!/bin/bash
# usage: ctl.sh '<python code; set result=...>'
curl -s -m 70 -X POST --data-binary "$1" http://127.0.0.1:8790/
