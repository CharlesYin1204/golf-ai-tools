package cn.gaoqiu.app;
import android.app.Activity;
import android.os.Bundle;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.webkit.*;
import android.widget.Toast;
public class MainActivity extends Activity {
 private WebView web; private ValueCallback<Uri[]> chooser; private PermissionRequest microphone;
 private boolean local(Uri u){return "https".equals(u.getScheme()) && "gaoqiu.yinchao1204.vip".equals(u.getHost()) && (u.getPort()==-1||u.getPort()==443);}
 @Override public void onCreate(Bundle b){super.onCreate(b);web=new WebView(this);setContentView(web);web.getSettings().setJavaScriptEnabled(true);web.getSettings().setDomStorageEnabled(true);web.getSettings().setAllowFileAccess(false);web.getSettings().setAllowContentAccess(true);web.getSettings().setMixedContentMode(android.webkit.WebSettings.MIXED_CONTENT_NEVER_ALLOW);CookieManager.getInstance().setAcceptCookie(true);
 web.setWebViewClient(new WebViewClient(){@Override public boolean shouldOverrideUrlLoading(WebView v,String url){Uri uri=Uri.parse(url);if(local(uri))return false;if("https".equals(uri.getScheme())||"http".equals(uri.getScheme())){try{startActivity(new Intent(Intent.ACTION_VIEW,uri));}catch(Exception e){Toast.makeText(MainActivity.this,"请安装浏览器后打开链接",Toast.LENGTH_SHORT).show();}}return true;}});
 web.setWebChromeClient(new WebChromeClient(){@Override public boolean onShowFileChooser(WebView v,ValueCallback<Uri[]> callback,FileChooserParams params){if(chooser!=null)chooser.onReceiveValue(null);chooser=callback;try{startActivityForResult(params.createIntent(),10);}catch(Exception e){chooser.onReceiveValue(null);chooser=null;}return true;}@Override public void onPermissionRequest(PermissionRequest r){if(!local(r.getOrigin())){r.deny();return;}for(String resource:r.getResources()){if(!PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(resource)){r.deny();return;}}if(checkSelfPermission(android.Manifest.permission.RECORD_AUDIO)==PackageManager.PERMISSION_GRANTED)r.grant(new String[]{PermissionRequest.RESOURCE_AUDIO_CAPTURE});else{if(microphone!=null)microphone.deny();microphone=r;requestPermissions(new String[]{android.Manifest.permission.RECORD_AUDIO},11);}}@Override public void onPermissionRequestCanceled(PermissionRequest r){if(microphone==r)microphone=null;}});
 web.setDownloadListener(new DownloadListener(){public void onDownloadStart(String url,String agent,String disposition,String mime,long length){Uri u=Uri.parse(url);if(local(u)){try{startActivity(new Intent(Intent.ACTION_VIEW,u));}catch(Exception e){Toast.makeText(MainActivity.this,"请使用浏览器下载",Toast.LENGTH_SHORT).show();}}}});
 if(b!=null)web.restoreState(b);else web.loadUrl("https://gaoqiu.yinchao1204.vip/");}
 @Override protected void onActivityResult(int request,int result,Intent data){super.onActivityResult(request,result,data);if(request==10&&chooser!=null){chooser.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(result,data));chooser=null;}}
 @Override public void onRequestPermissionsResult(int request,String[] names,int[] grants){super.onRequestPermissionsResult(request,names,grants);if(request==11&&microphone!=null){if(grants.length>0&&grants[0]==PackageManager.PERMISSION_GRANTED)microphone.grant(new String[]{PermissionRequest.RESOURCE_AUDIO_CAPTURE});else microphone.deny();microphone=null;}}
 @Override public void onBackPressed(){if(web.canGoBack())web.goBack();else super.onBackPressed();}
 @Override protected void onSaveInstanceState(Bundle b){web.saveState(b);super.onSaveInstanceState(b);}
 @Override protected void onDestroy(){if(chooser!=null)chooser.onReceiveValue(null);if(microphone!=null)microphone.deny();CookieManager.getInstance().flush();web.destroy();super.onDestroy();}
}
