"""Production defaults: real sessions, isolated records and server-owned privileges."""
import os,tempfile,threading,unittest,copy
from pathlib import Path
from backend_test import module,Client,ROOT
class ProductionTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.temp=tempfile.TemporaryDirectory();os.environ['GOLFERA_ADMIN_PASSWORD']='ProductionTest_5678'
  cls.db=module.Database(Path(cls.temp.name)/'app.sqlite3')
  cls.http=module.GolfServer(('127.0.0.1',0),cls.db,ROOT/'dist');cls.thread=threading.Thread(target=cls.http.serve_forever,daemon=True);cls.thread.start();cls.base=f'http://127.0.0.1:{cls.http.server_port}'
  cls.admin=Client(cls.base);cls.admin.data('/api/auth/login','POST',{'username':'admin','password':'ProductionTest_5678'})
 @classmethod
 def tearDownClass(cls):cls.http.shutdown();cls.http.server_close();cls.thread.join();cls.temp.cleanup()
 def account(self):
  import secrets
  name='user_'+secrets.token_hex(4);p='AccountPassword_5678';ident=self.admin.data('/api/admin/accounts','POST',{'username':name,'name':'球友','password':p},201)['id'];client=Client(self.base);client.data('/api/user/login','POST',{'username':name,'password':p});return client,ident,name,p
 def test_01_no_seed_or_anonymous_workspace(self):
  self.assertEqual(self.admin.data('/api/admin/courses')['total'],0)
  anon=Client(self.base)
  status,catalog,headers=anon.request('/api/public/catalog')
  self.assertEqual(status,200);self.assertEqual(catalog['data']['courses'],[]);self.assertNotIn('state',catalog['data']);self.assertNotIn('Set-Cookie',headers)
  self.assertEqual(anon.request('/api/mobile/state')[0],401)
  self.assertEqual(anon.request('/api/mobile/state','PUT',{'state':{}})[0],401)
  self.assertEqual(self.admin.request('/api/admin/seed','POST',{})[0],403)
  with self.db.connection() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM visitors').fetchone()[0],0)
 def test_02_empty_account_and_logout_revokes_token(self):
  a,ident,_,_=self.account();state=a.data('/api/mobile/state')['state']
  self.assertFalse(state['round']['course']);self.assertEqual(state['orders'],[]);self.assertEqual(state['history'],[])
  a.data('/api/mobile/state','PUT',{'state':state})
  token=next(c.value for c in a.cookies if c.name=='golfera_user')
  a.data('/api/user/logout','POST',{});self.assertEqual(a.request('/api/mobile/state')[0],401)
  import urllib.request,urllib.error
  with self.assertRaises(urllib.error.HTTPError) as e:urllib.request.urlopen(urllib.request.Request(self.base+'/api/mobile/state',headers={'Cookie':'golfera_user='+token}))
  self.assertEqual(e.exception.code,401)
 def test_03_password_rotation_and_other_sessions(self):
  a,_,name,password=self.account();b=Client(self.base);b.data('/api/user/login','POST',{'username':name,'password':password})
  self.assertEqual(a.request('/api/user/password','POST',{'old_password':'wrong','password':'NewPassword_12345'})[0],400)
  a.data('/api/user/password','POST',{'old_password':password,'password':'NewPassword_12345'})
  self.assertEqual(b.request('/api/mobile/state')[0],401)
  self.assertEqual(a.request('/api/user/login','POST',{'username':name,'password':password})[0],401)
  a.data('/api/user/login','POST',{'username':name,'password':'NewPassword_12345'})
 def test_04_booking_isolation_inventory_and_staff_permissions(self):
  course=self.admin.data('/api/admin/courses','POST',{'name':'回归球场','short':'回归','address':'测试地址','holes':18,'price':100,'distance':0,'tags':[],'status':'active'},201)['id']
  self.admin.data('/api/admin/slots','POST',{'course_id':course,'date':module.day(2),'time':'08:00','capacity':2,'price':100,'status':'open'},201)
  a,ident,_,_=self.account();b,_,_,_=self.account();state=a.data('/api/mobile/state')['state'];state['orders']=[{'id':'PROD_ORDER','course':course,'date':module.day(2),'slot':'08:00','people':2,'total':1,'status':'confirmed','checked':False}]
  saved=a.data('/api/mobile/state','PUT',{'state':state})['state'];self.assertEqual(saved['orders'][0]['total'],200)
  self.assertEqual(b.data('/api/mobile/state')['state']['orders'],[])
  bad=copy.deepcopy(saved);bad['role']='caddie';self.assertEqual(a.request('/api/mobile/state','PUT',{'state':bad})[0],403)
  bad=copy.deepcopy(saved);bad['orders'][0]['checked']=True;self.assertEqual(a.request('/api/mobile/state','PUT',{'state':bad})[0],403)
  other=b.data('/api/mobile/state')['state'];other['orders']=copy.deepcopy(saved['orders']);self.assertEqual(b.request('/api/mobile/state','PUT',{'state':other})[0],403)
  other['orders'][0]['id']='OTHER_ORDER';self.assertEqual(b.request('/api/mobile/state','PUT',{'state':other})[0],409)
  self.admin.data('/api/admin/orders/PROD_ORDER','PATCH',{'checked':True})
  saved=a.data('/api/mobile/state')['state'];self.assertTrue(saved['orders'][0]['checked'])
  bad=copy.deepcopy(saved);bad['bag']['stage']=1;self.assertEqual(a.request('/api/mobile/state','PUT',{'state':bad})[0],403)
  saved['orders'][0]['status']='cancelled';a.data('/api/mobile/state','PUT',{'state':saved});self.assertEqual(a.data('/api/public/slots?course_id='+course+'&date='+module.day(2))[0]['available'],2)
 def test_05_round_persistence_and_cross_account(self):
  a,_,_,_=self.account();b,_,_,_=self.account();course=self.admin.data('/api/admin/courses')['items'][0]['id'];state=a.data('/api/mobile/state')['state'];state['round'].update(course=course,scores=[4]+[None]*17)
  result=a.data('/api/mobile/state','PUT',{'state':state})['state'];self.assertEqual(a.data('/api/mobile/state')['state']['round']['scores'][0],4)
  other=b.data('/api/mobile/state')['state'];other['round']=result['round'];self.assertEqual(b.request('/api/mobile/state','PUT',{'state':other})[0],403)
if __name__=='__main__':unittest.main(verbosity=2)
