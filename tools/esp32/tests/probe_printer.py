#!/usr/bin/env python3
"""Execute installed-printer probe handling in the ARM peripheral model."""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[3]/'bootloader/tests'))
from model import Model, setup

cases={}
for stage in ('bootloader','application'):
    e=Model(actions=[[('stay',setup(0x40,0x40))]] if stage=='bootloader' else [])
    e.run(lambda x: bool(x.info()) and x.info()[2]==(1 if stage=='bootloader' else 2))
    before=e.resets
    e.command=0
    e.run(lambda x: x.results==[0x90],resume=True)
    assert e.results==[0x90] and e.resets==before and not e.erases and not e.programs
    cases[stage]={'result':'90','extra_resets':e.resets-before,'erases':e.erases,'programs':e.programs}
e=Model(command=0)
e.run(lambda x: bool(x.info()) and x.results==[0x90])
assert not e.erases and not e.programs and e.resets==1
cases['stage0']={'ignored_command_then_stage2_rejected':True,'erases':[],'programs':[]}
print(json.dumps({'cases':cases,'passed':True},indent=2))
