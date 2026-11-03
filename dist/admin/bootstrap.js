/* 后台启动兜底：资源或初始化失败时，不能永久停留在加载提示。 */
(() => {
  const showFailure = () => {
    const root = document.getElementById('root');
    if (!root?.querySelector('.initial-load')) return;
    root.innerHTML = '<section class="initial-load" role="alert"><h2>后台暂时未能加载</h2><p>可以重新加载管理页面。已保存的运营数据不会丢失。</p><a class="btn primary" href="/admin/">重新加载后台</a></section>';
  };
  setTimeout(showFailure, 12000);
  window.addEventListener('error', event => {
    if (event.target?.tagName === 'SCRIPT') showFailure();
  }, true);
  window.addEventListener('unhandledrejection', showFailure);
})();
