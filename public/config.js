// Сайт и API живут на одном Worker'е, поэтому адрес сервера = адрес сайта.
// Если сайт остаётся на Pages, а API на Worker'е, впиши сюда адрес Worker'а:
// window.MP_API_BASE='https://marketpulse.ИМЯ.workers.dev';
window.MP_API_BASE=location.origin;
