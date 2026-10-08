/* Load once in the cabinet; child forms share its authenticated Telegram context. */
window.telegramReady = new Promise((resolve, reject) => {
  try {
    if (window.parent !== window && window.parent.location.origin === location.origin && window.parent.Telegram?.WebApp) {
      window.Telegram = window.parent.Telegram;
      for (const [key, value] of Object.entries(window.Telegram.WebApp.themeParams || {})) {
        document.documentElement.style.setProperty('--tg-theme-' + key.replaceAll('_', '-'), value);
      }
      resolve();
      return;
    }
  } catch (_) { /* A standalone Telegram window has a cross-origin parent. */ }
  const script = document.createElement('script');
  script.src = 'https://telegram.org/js/telegram-web-app.js';
  script.async = true;
  const timer = setTimeout(() => reject(new Error('Telegram загружается дольше обычного. Проверьте соединение и повторите.')), 15000);
  script.onload = () => {
    clearTimeout(timer);
    if (window.Telegram?.WebApp) resolve();
    else reject(new Error('Не удалось загрузить Telegram. Повторите открытие окна.'));
  };
  script.onerror = () => {
    clearTimeout(timer);
    reject(new Error('Не удалось загрузить Telegram. Проверьте соединение.'));
  };
  document.head.append(script);
});
