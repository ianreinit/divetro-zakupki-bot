const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('webapp/telegram_bootstrap.js', 'utf8');
function setup(embedded) {
  const scripts = [], theme = {};
  const location = {origin:'https://example.test'};
  const window = {};
  window.parent = embedded ? {location, Telegram:{WebApp:{initData:'signed', themeParams:{bg_color:'#000000'}}}} : window;
  const context = {window, location, setTimeout, clearTimeout, document:{
    createElement:()=>({}), head:{append:s=>scripts.push(s)},
    documentElement:{style:{setProperty:(k,v)=>theme[k]=v}}}};
  vm.runInNewContext(source,context);
  return {window,scripts,theme};
}
(async()=>{
  const child=setup(true);
  await child.window.telegramReady;
  assert.equal(child.scripts.length,0);
  assert.equal(child.window.Telegram.WebApp.initData,'signed');
  assert.equal(child.theme['--tg-theme-bg-color'],'#000000');
  const top=setup(false);
  assert.equal(top.scripts.length,1);
  assert.equal(top.scripts[0].async,true);
  top.window.Telegram={WebApp:{}};
  top.scripts[0].onload();
  await top.window.telegramReady;
  const failed=setup(false);
  failed.scripts[0].onerror();
  await assert.rejects(failed.window.telegramReady,/Не удалось загрузить Telegram/);
  console.log('Telegram bootstrap: 3 scenarios passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
