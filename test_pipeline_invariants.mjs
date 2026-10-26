import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import test from 'node:test';
import {resolvePython} from './test_support/python-interpreter.mjs';
import {writeFileSync} from 'node:fs';

test('Python publisher → JSON → web renderer: single/pair degradations, healthy exact values and frozen capture times',
  {skip: process.platform === 'win32' ? 'Requires POSIX fcntl imported by server.py in the Mac publisher' : false}, (t) => {
  const python = resolvePython(t); if (!python) return;
  let output;
  try { output=execFileSync(python.command, [...python.args, '-B','test_pipeline_invariants.py','--check'],
    {cwd:new URL('.',import.meta.url),encoding:'utf8',timeout:180000,maxBuffer:16*1024*1024}); }
  catch(error){throw new Error(error.stdout||error.message);}
  const report=JSON.parse(output);
  if(process.env.AGIW_A4_REPORT)writeFileSync(process.env.AGIW_A4_REPORT,JSON.stringify(report,null,2)+"\n");
  assert.equal(report.baseCases,5938);
  assert.equal(report.cases,6721);
  assert.equal(report.rendered,6721);
  assert.equal(report.dimensions['healthy-exact'].cases,19);
  assert.equal(report.dimensions['paused-view'].cases,76);
  assert.equal(report.dimensions['transport-age-boundary'].cases,63);
  assert.equal(report.dimensions['event-age-is-history'].cases,105);
  assert.deepEqual(report.violations,[]);
});
