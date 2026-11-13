
const $ = (selector) => document.querySelector(selector);
const main = $("#main");
const categories = ["Кино", "Природа", "Путешествия", "Музыка", "Технологии", "Другое"];
const icons = {
  menu: "M4 6h16M4 12h16M4 18h16", search: "M21 21l-4.4-4.4M19 11a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z",
  home: "m3 10 9-7 9 7v10H3zM9 20v-6h6v6", subs: "M5 3h14M3 7h18M3 11h18v10H3zM10 14l5 2-5 2z",
  library: "M4 6v14h14M8 3h12v13H8zM12 7l4 2.5L12 12z", lock: "M6 11h12v10H6zM8 11V7a4 4 0 0 1 8 0v4",
  plus: "M12 5v14M5 12h14", user: "M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2m12-14a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z",
  shield: "M12 22s7-3 7-9V5l-7-3-7 3v8c0 6 7 9 7 9Z", upload: "M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5",
  flag: "M5 22V3c5-4 9 4 15 0v11c-6 4-10-4-15 0", like: "M7 10v11H3V10zm0 10h11a2 2 0 0 0 2-2l1-6a2 2 0 0 0-2-2h-5l1-5a2 2 0 0 0-2-2l-6 7",
  film: "M3 4h18v16H3zM7 4v16M17 4v16M3 9h4m-4 6h4m10-6h4m-4 6h4",
  history: "M3 12a9 9 0 1 1 2.7 6.4M3 5v7h7m2-5v5l3 2",
  arrow: "m9 5 7 7-7 7", code: "m8 6-6 6 6 6m8-12 6 6-6 6", logout: "M10 3H4v18h6m4-14 5 5-5 5m5-5H8",
};
const icon = (name) => `<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round"><path d="${icons[name]}"/></svg>`;
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const number = (value) => new Intl.NumberFormat("ru-RU", {notation: "compact"}).format(value);
const date = (value) => new Date(value).toLocaleDateString("ru-RU", {day:"numeric",month:"short",year:"numeric"});
const duration = (seconds) => {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor(seconds / 60) % 60;
  return (hours ? hours + ":" + String(minutes).padStart(2, "0") : minutes) + ":" + String(seconds % 60).padStart(2, "0");
};
const channelURL = (handle) => "/channel/" + encodeURIComponent(handle);
const videoAPI = (id) => "/api/videos/" + id;
const avatar = (handle, large = false) => {
  const hue = [...handle].reduce((sum, c) => sum * 31 + c.charCodeAt(0) >>> 0, 0) % 360;
  return `<span class="avatar ${large ? "avatar-large" : ""}" style="--hue:${hue}" aria-hidden="true">${esc(handle[0]?.toUpperCase())}</span>`;
};
let user = null;
let generation = 0;
let toastTimer;
let pendingUpload = false;

async function api(url, method = "GET", data) {
  const response = await fetch(url, {
    method,
    headers: data === undefined ? {} : {"Content-Type":"application/json"},
    body: data === undefined ? undefined : JSON.stringify(data)
  });
  if (response.status === 204) return null;
  const result = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = result?.detail;
    const error = new Error(typeof detail === "string" ? detail : response.status === 422 ? "Проверьте введённые данные" : "Ошибка запроса");
    error.status = response.status;
    throw error;
  }
  return result;
}

function toast(message) {
  const box = $("#toast");
  box.textContent = message;
  box.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => box.classList.remove("visible"), 4200);
}

function go(url, replace = false) {
  if (pendingUpload && !confirm("Прервать загрузку?")) return;
  if (pendingUpload) pendingUpload.abort();
  history[replace ? "replaceState" : "pushState"]({}, "", url);
  document.body.classList.remove("nav-open");
  window.scrollTo(0, 0);
  render();
}
function loginURL() {
  return "/login?next=" + encodeURIComponent(location.pathname + location.search);
}
function requireUser() {
  if (user) return true;
  go(loginURL());
  return false;
}
function bind(selector, event, callback) {
  $(selector)?.addEventListener(event, async (e) => {
    try { await callback(e); }
    catch (error) {
      if (error.status === 401) go(loginURL());
      else toast(error.message);
    }
  });
}
function link(url, text, name) {
  return `<a data-nav href="${url}" ${location.pathname === url ? 'class="active" aria-current="page"' : ""}>${icon(name)}<span>${text}</span></a>`;
}
function shell() {
  const q = location.pathname === "/search" ? new URLSearchParams(location.search).get("q") : "";
  $("#header").innerHTML = `
    <div class="brand-group"><button class="icon-button" id="menu" aria-label="Меню" aria-controls="sidebar" aria-expanded="${document.body.classList.contains("nav-open") || !matchMedia("(max-width: 800px)").matches && !document.body.classList.contains("nav-compact")}">${icon("menu")}</button>
    <a href="/" class="brand" data-nav><img src="/static/favicon.svg" alt="" width="36" height="36">YourTube</a></div>
    <form class="search" id="search" role="search"><input type="search" name="q" maxlength="120" value="${esc(q)}" placeholder="Поиск видео" aria-label="Поиск видео" required><button aria-label="Найти">${icon("search")}</button></form>
    <div class="header-actions"><a data-nav class="button create" href="/upload" aria-label="Загрузить">${icon("plus")}<span>Загрузить</span></a>
    ${user ? `<a data-nav class="profile" href="${channelURL(user.handle)}" aria-label="Ваш канал">${avatar(user.handle)}</a>`
           : `<a data-nav class="button login" aria-label="Войти" href="${loginURL()}">${icon("user")}<span>Войти</span></a>`}</div>`;
  $("#sidebar").innerHTML = `
    <nav>${link("/", "Главная", "home")}${link("/subscriptions", "Подписки", "subs")}</nav>
    <nav><p>Вы</p>${link("/mine", "Мои видео", "library")}${link("/shared", "Доступные мне", "lock")}
    ${link("/liked", "Понравившиеся", "like")}${link("/history", "История просмотра", "history")}
    ${user ? link(channelURL(user.handle), "Мой канал", "user") : ""}</nav>
    <nav>${user && user.role !== "user" ? link("/moderation", "Модерация", "shield") : ""}
    <a href="/docs">${icon("code")}<span>Swagger API</span></a>
    ${user ? `<button id="logout">${icon("logout")}<span>Выйти</span></button>` : ""}</nav>`;
  bind("#menu", "click", e => {
    const mobile = matchMedia("(max-width: 800px)").matches;
    const toggled = document.body.classList.toggle(mobile ? "nav-open" : "nav-compact");
    e.currentTarget.setAttribute("aria-expanded", mobile ? toggled : !toggled);
  });
  bind("#search", "submit", e => {
    e.preventDefault();
    const q = new FormData(e.target).get("q").trim();
    if (q) go("/search?" + new URLSearchParams({q}));
  });
  bind("#logout", "click", async () => { await api("/api/logout", "POST"); user = null; go("/", true); });
}

function empty(title, copy = "", name = "film") {
  return `<div class="empty"><span class="empty-icon">${icon(name)}</span><h2>${title}</h2>${copy ? `<p>${copy}</p>` : ""}</div>`;
}
function signedOut(title) {
  return `<section class="empty"><span class="empty-icon">${icon("lock")}</span><h2>${title}</h2>
    <p>Войдите в аккаунт, чтобы продолжить.</p><a data-nav class="button primary" href="${loginURL()}">Войти</a></section>`;
}
function card(v, manage = false, playlist = null) {
  const href = manage ? "/manage/" + v.id : "/watch/" + v.id;
  return `<article class="card">
    <a data-nav class="thumbnail" href="${href}" aria-label="${esc(v.title)}">
      ${v.hidden ? icon("shield") : `<img src="${videoAPI(v.id)}/preview" alt="" loading="lazy">`}
      ${v.hidden || v.private ? `<span class="visibility">${v.hidden ? "Скрыто" : "Приватное"}</span>` : ""}
      <time>${duration(v.duration)}</time>
    </a>
    <div class="card-info"><a data-nav href="${channelURL(v.handle)}" aria-label="Канал ${esc(v.handle)}">${avatar(v.handle)}</a>
      <div><a class="card-title" data-nav href="${href}">${esc(v.title)}</a>
      <a class="muted channel-name" data-nav href="${channelURL(v.handle)}">@${esc(v.handle)}</a>
      <p class="muted">${number(v.views)} просм. <span>·</span> ${date(v.created_at)}</p>
      ${playlist ? `<button class="text-button remove-video" data-id="${v.id}">Убрать из плейлиста</button>` : ""}</div></div>
  </article>`;
}
function grid(videos, manage = false, playlist = null) {
  return `<div class="grid">${videos.map(v => card(v, manage, playlist)).join("")}</div>`;
}
function more(count, limit = 24) {
  return count === limit ? '<button class="button more" id="more">Показать ещё</button>' : "";
}
function pagination(count, fetchNext, append, limit = 24) {
  let offset = count;
  bind("#more", "click", async e => {
    const button = e.currentTarget;
    button.disabled = true;
    try {
      const rows = await fetchNext(offset);
      if (!button.isConnected) return;
      append(rows);
      offset += rows.length;
      if (rows.length < limit) button.remove();
    } finally { button.disabled = false; }
  });
}

async function feed(path, query, current) {
  const mode = ({"/subscriptions":"subscriptions","/mine":"mine","/shared":"shared","/liked":"liked","/history":"history"})[path] ?? "home";
  if (mode !== "home" && !user) return signedOut("Этот раздел доступен после входа");
  const search = path === "/search";
  const title = search ? "Результаты поиска" : ({home:"Главная",subscriptions:"Подписки",mine:"Мои видео",shared:"Доступные мне",liked:"Понравившиеся",history:"История просмотра"})[mode];
  const q = search ? query.get("q") ?? "" : "";
  const selected = query.get("category") ?? "";
  const params = new URLSearchParams({feed:mode,q,category:selected});
  const fetchNext = offset => api("/api/videos?" + new URLSearchParams({...Object.fromEntries(params), offset}));
  const videos = await fetchNext(0);
  if (!current()) return;
  const pills = path === "/" ? `<div class="pills" aria-label="Категории">${["", ...categories].map(c =>
    `<a data-nav href="${c ? "/?" + new URLSearchParams({category:c}) : "/"}" class="${c === selected ? "selected" : ""}">${c || "Все видео"}</a>`).join("")}</div>` : "";
  main.innerHTML = `<div class="page-heading"><div><h1>${title}</h1>${search ? `<p>По запросу «${esc(q)}»</p>` : ""}</div>
    ${mode === "mine" ? '<a data-nav class="button primary" href="/upload">Загрузить видео</a>' : ""}</div>
    ${pills}${videos.length ? grid(videos, mode === "mine") + more(videos.length) :
      empty(search ? "Видео не найдены" : ({liked:"У вас пока нет понравившихся видео",history:"История просмотра пуста"})[mode] ?? "Здесь пока нет видео", search ? "Попробуйте другой запрос." : "", search ? "search" : "film")}`;
  pagination(videos.length, fetchNext, rows => $(".grid").insertAdjacentHTML("beforeend", rows.map(v => card(v, mode === "mine")).join("")));
}

function metaFields(video = {}) {
  return `<label>Название<input name="title" required maxlength="120" value="${esc(video.title)}" placeholder="Название видео"></label>
    <label>Описание<textarea name="description" maxlength="5000" rows="4" placeholder="Расскажите о видео">${esc(video.description)}</textarea></label>
    <div class="form-row"><label>Категория<select name="category">${categories.map(c =>
      `<option ${c === (video.category || "Другое") ? "selected" : ""}>${c}</option>`).join("")}</select></label>
    <label>Доступ<select name="private"><option value="false">Для всех</option>
      <option value="true" ${video.private ? "selected" : ""}>Приватное</option></select></label></div>`;
}
function metadata(form) {
  const {title, description, category, private: privacy} = Object.fromEntries(new FormData(form));
  return {title, description, category, private: privacy === "true"};
}
function upload() {
  if (!user) return signedOut("Войдите, чтобы загрузить видео");
  main.innerHTML = `<div class="page-heading"><div><h1>Загрузка видео</h1><p>Добавьте видео на свой канал.</p></div></div>
    <form class="panel stack form-wide" id="upload">
      <label class="drop" id="drop">${icon("upload")}<strong id="filename">Выберите видеофайл</strong><span>Или перетащите его сюда</span>
      <input type="file" name="file" accept="video/*,.mkv,.avi,.mov" required></label>
      ${metaFields()}<div id="upload-progress" hidden><progress max="100" value="0" aria-label="Прогресс загрузки"></progress><p class="muted" aria-live="polite"></p></div>
      <p id="upload-error" class="error" role="alert" hidden></p><button class="button primary" type="submit">Опубликовать</button>
    </form>`;
  const input = $("#upload input[type=file]");
  const drop = $("#drop");
  const setName = () => { $("#filename").textContent = input.files[0]?.name || "Выберите видеофайл"; };
  input.addEventListener("change", setName);
  drop.addEventListener("dragover", e => { e.preventDefault(); drop.classList.add("dragging"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("dragging"));
  drop.addEventListener("drop", e => {
    e.preventDefault();
    drop.classList.remove("dragging");
    if (e.dataTransfer.files.length) { input.files = e.dataTransfer.files; setName(); }
  });
  bind("#upload", "submit", e => {
    e.preventDefault();
    if (pendingUpload) return;
    const progress = $("#upload-progress");
    const meter = progress.querySelector("progress");
    const status = progress.querySelector("p");
    const errorBox = $("#upload-error");
    const button = e.target.querySelector("button");
    const xhr = new XMLHttpRequest();
    pendingUpload = xhr;
    progress.hidden = false;
    errorBox.hidden = true;
    meter.value = 0;
    status.textContent = "Загрузка…";
    button.disabled = true;
    xhr.open("POST", "/api/videos?" + new URLSearchParams(metadata(e.target)));
    xhr.responseType = "json";
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable) return;
      const pct = Math.floor(event.loaded / event.total * 100);
      meter.value = pct;
      status.textContent = "Загрузка… " + pct + "%";
    };
    xhr.upload.onload = () => { meter.removeAttribute("value"); status.textContent = "Видео обрабатывается…"; };
    const finish = () => { pendingUpload = false; button.disabled = false; };
    const error = (message) => { finish(); progress.hidden = true; errorBox.hidden = false; errorBox.textContent = message; };
    xhr.onerror = () => error("Соединение прервалось. Попробуйте снова.");
    xhr.onabort = finish;
    xhr.onload = () => {
      if (xhr.status === 201) { finish(); toast("Видео опубликовано"); go("/manage/" + xhr.response.id); }
      else if (xhr.status === 401) { finish(); go(loginURL()); }
      else error(typeof xhr.response?.detail === "string" ? xhr.response.detail : "Проверьте файл и метаданные.");
    };
    xhr.send(input.files[0]);
  });
}

function auth(path, query) {
  const register = path === "/register";
  const next = query.get("next") || "/";
  const safe = /^\/(?![/\\])[^\\]*$/.test(next) ? next : "/";
  if (user) { queueMicrotask(() => go(safe, true)); return; }
  const tail = "?" + new URLSearchParams({next:safe});
  main.innerHTML = `<section class="auth panel stack"><img src="/static/favicon.svg" width="48" height="48" alt="">
    <h1>${register ? "Создать аккаунт" : "Вход в YourTube"}</h1>
    <form id="auth" class="stack">
    <label>Имя пользователя<input name="handle" autocomplete="username" placeholder="например, film_lab" pattern="[A-Za-z0-9_]{3,30}" required>
    ${register ? "<small>От 3 до 30 латинских букв, цифр или знаков подчёркивания.</small>" : ""}</label>
    <label>Пароль<input type="password" name="password" autocomplete="${register ? "new-password" : "current-password"}" minlength="8" maxlength="128" required></label>
    <p id="auth-error" class="error" role="alert" hidden></p><button class="button primary">${register ? "Зарегистрироваться" : "Войти"}</button>
    </form><p class="muted">${register ? "Уже есть аккаунт?" : "Впервые здесь?"} <a class="accent" data-nav href="${register ? "/login" : "/register"}${esc(tail)}">${register ? "Войти" : "Зарегистрироваться"}</a></p></section>`;
  bind("#auth", "submit", async e => {
    e.preventDefault();
    const button = e.target.querySelector("button");
    const errorBox = $("#auth-error");
    button.disabled = true;
    try { user = await api("/api" + path, "POST", Object.fromEntries(new FormData(e.target))); go(safe, true); }
    catch (error) { errorBox.hidden = false; errorBox.textContent = error.message; }
    finally { button.disabled = false; }
  });
}

