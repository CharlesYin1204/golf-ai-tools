import unittest
from backend_test import BackendTests,Client
class AccountsTests(BackendTests):
 def test_account_login_and_isolation(self):
  ident=self.admin.data('/api/admin/accounts','POST',{'username':'qa_user','name':'测试用户','password':'TestPassword_5678','status':'active'},201)['id']
  first=Client(self.base);second=Client(self.base)
  self.assertEqual(first.request('/api/user/login','POST',{'username':'qa_user','password':'bad'})[0],401)
  first.data('/api/user/login','POST',{'username':'qa_user','password':'TestPassword_5678'})
  second.data('/api/user/login','POST',{'username':'qa_user','password':'TestPassword_5678'})
  self.assertEqual(first.data('/api/user/me')['user']['id'],ident)
  state=first.data('/api/mobile/state')['state'];self.assertEqual(state['orders'],[]);self.assertEqual(state['history'],[])
  state['favorites']=['qizhong'];first.data('/api/mobile/state','PUT',{'state':state})
  self.assertEqual(second.data('/api/mobile/state')['state']['favorites'],['qizhong'])
  self.assertEqual(first.request('/api/mobile/reset','POST',{})[0],403)
  first.data('/api/user/logout','POST',{})
  self.assertIsNone(first.data('/api/user/me')['user'])
  self.admin.data('/api/admin/accounts','PATCH',{'id':ident,'status':'inactive'})
  self.assertEqual(second.request('/api/mobile/state')[0],401)
  self.assertEqual(second.request('/api/user/login','POST',{'username':'qa_user','password':'TestPassword_5678'})[0],401)
 def test_account_admin_only_and_password_reset(self):
  anon=Client(self.base)
  self.assertEqual(anon.request('/api/admin/accounts')[0],401)
  ident=self.admin.data('/api/admin/accounts','POST',{'username':'qa_reset','name':'重置测试','password':'OriginalPass_5678'},201)['id']
  anon.data('/api/user/login','POST',{'username':'qa_reset','password':'OriginalPass_5678'})
  self.admin.data('/api/admin/accounts','PATCH',{'id':ident,'status':'active','password':'ReplacedPass_5678'})
  self.assertEqual(anon.request('/api/user/me')[0],401)
  self.assertEqual(anon.request('/api/user/login','POST',{'username':'qa_reset','password':'OriginalPass_5678'})[0],401)
  anon.data('/api/user/login','POST',{'username':'qa_reset','password':'ReplacedPass_5678'})
  self.assertNotIn('password',str(self.admin.data('/api/admin/accounts')))
  with self.db.connection() as c:
   v=c.execute('SELECT id FROM visitors WHERE user_id=?',(ident,)).fetchone()['id']
   encoded=c.execute('SELECT password_hash FROM user_accounts WHERE user_id=?',(ident,)).fetchone()[0]
  self.assertNotIn('ReplacedPass',encoded)
  import urllib.request,urllib.error
  request=urllib.request.Request(self.base+'/api/mobile/state',headers={'Cookie':'golfera_visitor='+v})
  with self.assertRaises(urllib.error.HTTPError) as e:urllib.request.urlopen(request)
  self.assertEqual(e.exception.code,401)
if __name__=='__main__':
 suite=unittest.TestSuite(AccountsTests(n) for n in unittest.defaultTestLoader.getTestCaseNames(AccountsTests) if n.startswith('test_account_'))
 result=unittest.TextTestRunner(verbosity=2).run(suite);raise SystemExit(not result.wasSuccessful())
