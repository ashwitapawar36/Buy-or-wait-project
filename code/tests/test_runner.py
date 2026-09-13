"""Offline integration checks. Mock Gemini responses are NOT live model evaluation."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import main as runner
from buy_or_wait.evidence import empty_evidence
from buy_or_wait.data import Dataset

DATA=Path(__file__).resolve().parents[2]/'dataset'

class RunnerTests(unittest.TestCase):
    def test_sample_pipeline_with_mocked_transport(self):
        # Source-derived fixture for the first public example, with no sample output labels.
        dataset=Dataset(DATA);req=dataset.tables['sample_requests'][0]
        ctx=dataset.context(req)
        self.assertFalse(ctx['messages']);self.assertFalse(ctx['images'])
        scheduled=[e for e in ctx['events'] if e['direction']=='credit' and e['status']=='scheduled']
        ev=empty_evidence()
        for e in scheduled:
            ev['income_streams'].append({'source_ids':[e['event_id']],'amount':e['amount'],
                'currency':e['currency'],'first_date':e['settlement_date'],'frequency':'monthly',
                'interval_days':0,'end_date':'','reason':'Synthetic API fixture from confirmed salary row'})
        response={'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':json.dumps(ev)}]}}],
                  'usageMetadata':{'promptTokenCount':100,'candidatesTokenCount':50,'totalTokenCount':150}}
        class FakeResponse(io.BytesIO):
            def __enter__(self):return self
            def __exit__(self,*args):self.close()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(runner,'ROOT',root),patch.dict(os.environ,{'GEMINI_API_KEY':'test-key',
                    'GEMINI_MIN_INTERVAL_SECONDS':'0','GEMINI_INPUT_USD_PER_MILLION':'0','GEMINI_OUTPUT_USD_PER_MILLION':'0'}),\
                    patch('urllib.request.urlopen',return_value=FakeResponse(json.dumps(response).encode())) as transport,\
                    contextlib.redirect_stdout(io.StringIO()):
                code=runner.main(['--dataset',str(DATA),'--samples','--limit','1'])
            self.assertEqual(code,0);self.assertEqual(transport.call_count,1)
            self.assertFalse((root/'output.csv').exists())
            run=next((root/'runs').iterdir())
            self.assertTrue((run/'sample_scores.json').is_file())
            self.assertTrue((run/'audit'/f"{req['request_id']}.json").is_file())
            report=(run/'usage_report.md').read_text()
            self.assertIn('100',report);self.assertIn('150',report)
            self.assertFalse(json.loads((run/'manifest.json').read_text())['complete'])
            # Repeat with cached evidence; no transport permitted.
            with patch.object(runner,'ROOT',root),patch('urllib.request.urlopen',side_effect=AssertionError('network forbidden')),\
                    contextlib.redirect_stdout(io.StringIO()):
                code=runner.main(['--dataset',str(DATA),'--samples','--limit','1','--offline'])
            self.assertEqual(code,0)

    def test_failed_run_never_overwrites_old_final(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'output.csv').write_text('previous-complete-output')
            with patch.object(runner,'ROOT',root),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                result=runner.main(['--dataset',str(DATA),'--offline'])
            self.assertEqual(result,1)
            self.assertEqual((root/'output.csv').read_text(),'previous-complete-output')

if __name__=='__main__':unittest.main()
