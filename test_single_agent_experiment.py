"""Offline checks for the experimental tool loop; live quality is reviewed separately."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock
from scripts.single_agent_experiment import Experiment, CASES, PHOTO_TOOL, SYSTEM_PROMPT


def response(output, text=''):
    mock=MagicMock(status='completed',output_text=text)
    mock.model_dump.return_value={'id':'test','status':'completed','model':'test','output':output}
    return mock


class TestSingleAgentExperiment(unittest.TestCase):
    def test_preserves_50_case_order_and_adds_eight_checks(self):
        self.assertEqual([len(CASES[k]) for k in ['original40','new10','scope_checks','photo_checks']],[40,10,4,4])
        self.assertEqual(len(SYSTEM_PROMPT.splitlines()),3)
        self.assertEqual(len({x['id'] for group in CASES.values() for x in group}),58)

    def test_photo_tool_executes_and_continues_agent_without_logging_image_or_reasoning(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'photo.png';path.write_bytes(b'fixture image bytes')
            client=MagicMock()
            call={'type':'function_call','name':'analyze_photo','arguments':json.dumps({'image_id':'photo_1','question':'inspect'}),'call_id':'call_1','id':'function_1'}
            photo={'observations':['A dog'],'limitations':['No diagnosis'],'urgency':'uncertain','actions':[],'summary':'A dog','usable_animal_photo':True}
            client.responses.create.side_effect=[
                response([{'type':'reasoning','encrypted_content':'opaque-secret'},call]),
                response([{'type':'message','content':[{'type':'output_text','text':json.dumps(photo)}]}],json.dumps(photo)),
                response([{'type':'message','content':[{'type':'output_text','text':'A dog; health is uncertain.'}]}],'A dog; health is uncertain.'),
            ]
            history=[]
            row=Experiment(client,model='test',vision_model='test',image_root=directory).turn({'id':'P','question':'inspect','image':'photo.png'},history)
            self.assertEqual(row['status'],'completed')
            self.assertEqual(client.responses.create.call_count,3)
            self.assertEqual(sum(s['stage']=='photo_analysis' for s in row['trace']),1)
            self.assertNotIn('opaque-secret',json.dumps(row))
            self.assertNotIn('data:image',json.dumps(row))
            self.assertEqual(client.responses.create.call_args_list[0].kwargs['tools'][1],PHOTO_TOOL)
            self.assertTrue(any(i.get('type')=='function_call_output' for i in client.responses.create.call_args_list[2].kwargs['input']))

    def test_unknown_photo_id_cannot_read_a_path_or_call_vision(self):
        client=MagicMock()
        experiment=Experiment(client,model='test',vision_model='test',image_root='/tmp')
        self.assertIn('error',experiment.analyze_photo({'image_id':'../../secret','question':'inspect'},{},float('inf'),[]))
        client.responses.create.assert_not_called()

    def test_api_errors_are_recorded_as_errors_not_success(self):
        client=MagicMock();client.responses.create.side_effect=TimeoutError()
        row=Experiment(client,model='test',vision_model='test',image_root='/tmp').turn({'id':'Q','question':'Find care'},[])
        self.assertEqual(row['status'],'error');self.assertEqual(row['error']['type'],'TimeoutError')
        self.assertEqual(row['verdict'],'UNREVIEWED')

if __name__=='__main__':unittest.main()
