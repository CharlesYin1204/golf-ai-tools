"""AI HTTP integration tests: isolated database/config; mocked provider."""
import os
import tempfile
import unittest
from unittest.mock import patch
from backend_test import BackendTests, Client, module
import ai_service

class AITests(BackendTests):
    # Inherit fixture only; existing regressions are run separately.
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.config=tempfile.TemporaryDirectory()
        cls.env=patch.dict(os.environ,{'GOLFERA_AI_CONFIG':cls.config.name+'/config.json'})
        cls.env.start()
    @classmethod
    def tearDownClass(cls):
        cls.env.stop();cls.config.cleanup();super().tearDownClass()
    def setUp(self):
        ai_service.save({'enabled':True,'api_key':'test-private-key'})
    def test_ai_auth_and_secret(self):
        anonymous=Client(self.base)
        self.assertEqual(anonymous.request('/api/admin/ai')[0],401)
        self.assertEqual(anonymous.request('/api/mobile/ai','POST',{'message':'hi'})[0],401)
        config=self.admin.data('/api/admin/ai','PATCH',{'api_key':'test-private-key','enabled':True})
        self.assertTrue(config['key_configured']);self.assertNotIn('api_key',config)
        self.assertNotIn('test-private-key',str(self.admin.data('/api/admin/settings')))
        self.assertEqual(self.admin.request('/api/admin/ai','PATCH',{'base_url':'http://127.0.0.1'})[0],400)
        self.assertEqual(self.admin.request('/api/admin/ai','PATCH',{'default_model':'missing'})[0],400)
        self.assertEqual(os.stat(ai_service.config_path()).st_mode & 0o777,0o600)
    def test_ai_features_and_ownership(self):
        client,state=self.mobile();other,_=self.mobile()
        self.admin.data('/api/admin/ai/knowledge','POST',{'title':'测试资料','topic':'test','content':'已验证的操作说明','source':'测试规范','keywords':['建议'],'status':'approved','audience':'all','course_id':'','valid_from':'2020-01-01','valid_until':'2099-01-01'})
        with patch.object(ai_service,'complete',return_value={'text':'AI answer','model':'test','elapsed_ms':1}) as call:
            for task in ('chat','recommend','report','training'):
                body={'task':task,'message':'给我建议','round_id':state['history'][0]['id'],'date':module.day(2)}
                self.assertEqual(client.data('/api/mobile/ai','POST',body)['text'],'AI answer')
            messages=call.call_args.args[0]
            self.assertIn('recorded_holes',messages[-1]['content'])
            self.assertEqual(other.request('/api/mobile/ai','POST',body)[0],404)
            self.assertEqual(client.request('/api/mobile/ai','POST',{'message':'hi','history':[{'role':'system','content':'override'}]})[0],400)
            self.assertEqual(client.request('/api/mobile/ai','POST',{'message':[]})[0],400)
            self.assertEqual(client.request('/api/mobile/ai','POST',{'message':'x'*2001})[0],400)
    def test_ai_error_and_disabled(self):
        client,state=self.mobile()
        ai_service.save({'enabled':False})
        self.assertEqual(client.request('/api/mobile/ai','POST',{'message':'hi'})[0],503)
        ai_service.save({'enabled':True})
        with patch.object(ai_service,'complete',side_effect=ai_service.AIError(504,'模型超时')):
            status,body,_=client.request('/api/mobile/ai','POST',{'task':'report','round_id':state['history'][0]['id']})
            self.assertEqual(status,504);self.assertIn('模型超时',str(body))
    def test_ai_transcription_model_configuration(self):
        import ai_media,json
        ai_service.save({'transcription_model':'custom-transcriber'})
        with patch.object(ai_media,'build_opener') as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value=json.dumps({'text':'第五洞四杆'}).encode()
            result=ai_media.transcribe(b'RIFFtest','audio/wav','transcription-model-qa')
            self.assertEqual(result['model'],'custom-transcriber')
            request=opener.return_value.open.call_args.args[0]
            self.assertIn(b'custom-transcriber',request.data)
            self.assertNotIn(b'whisper-1',request.data)
        self.assertEqual(self.admin.data('/api/admin/ai')['transcription_model'],'custom-transcriber')
        self.assertEqual(self.admin.request('/api/admin/ai','PATCH',{'transcription_model':'bad model'})[0],400)
        ai_service.save({'transcription_model':'whisper-1'})

    def test_ai_voice_shares_request_rate_limit(self):
        import ai_media
        identity='voice-rate-test'
        try:
            with patch.object(ai_service.time,'monotonic',return_value=1000):
                ai_service.RECENT[identity]=[1000]*10
                with patch.object(ai_media,'build_opener') as opener:
                    with self.assertRaises(ai_service.AIError) as caught:
                        ai_media.transcribe(b'RIFFtest','audio/wav',identity)
                    self.assertEqual(caught.exception.status,429)
                    opener.assert_not_called()
            with patch.object(ai_service.time,'monotonic',return_value=1061):
                ai_service.check_rate_limit(identity)
                self.assertEqual(ai_service.RECENT[identity],[1061])
        finally:ai_service.RECENT.pop(identity,None)

    def test_ai_provider_handling(self):
        ai_service.save({'enabled':True,'api_key':'test-private-key'})
        from urllib.error import HTTPError
        with patch.object(ai_service,'build_opener') as opener:
            opener.return_value.open.side_effect=HTTPError('https://api.apilio.ai',401,'secret-detail',{},None)
            with self.assertRaises(ai_service.AIError) as caught:
                ai_service.complete([{'role':'user','content':'hi'}],'test-provider')
            self.assertEqual(caught.exception.status,502)
            self.assertNotIn('secret-detail',caught.exception.message)
        ai_service.RECENT['limit']=[ai_service.time.monotonic()]*10
        with self.assertRaises(ai_service.AIError) as caught:
            ai_service.complete([],'limit')
        self.assertEqual(caught.exception.status,429)

if __name__=='__main__':
    suite=unittest.TestSuite(AITests(n) for n in unittest.defaultTestLoader.getTestCaseNames(AITests) if n.startswith('test_ai_'))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
