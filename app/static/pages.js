
async function watch(id, current) {
  const url = videoAPI(id);
  const [v, comments, library] = await Promise.all([
    api(url), api(url + "/comments"), user ? api("/api/channels/" + user.handle) : null
  ]);
  if (!current()) return;
  const owner = user?.id === v.owner_id;
  const commentHTML = c => `<article class="comment">${avatar(c.handle)}<div><b>@${esc(c.handle)}</b> <small>${date(c.created_at)}</small><p>${esc(c.body)}</p></div></article>`;
  main.innerHTML = `<section class="watch"><video id="player" controls playsinline preload="metadata" poster="${url}/preview" src="${url}/stream"></video>
  <h1>${esc(v.title)}</h1><div class="watch-actions"><a data-nav class="channel-link" href="${channelURL(v.handle)}">${avatar(v.handle)}<span>@${esc(v.handle)}<small id="subscribers">${number(v.subscribers)} подписчиков</small></span></a>
  ${owner ? `<a data-nav class="button" href="/manage/${id}">Управление</a>` : '<button id="subscribe" class="button primary"></button>'}
  <button id="like" class="button"></button><button id="report-button" class="button" aria-label="Пожаловаться">${icon("flag")} Жалоба</button></div>
  <div class="panel description"><b><span id="views">${number(v.views)}</span> просм. · ${date(v.created_at)}${v.private ? " · Приватное" : ""}</b><p>${esc(v.description)}</p></div>
  ${library?.playlists.length ? `<form id="save-video" class="inline"><label>Сохранить в плейлист<select name="playlist">${library.playlists.map(p => `<option value="${p.id}">${esc(p.title)}</option>`).join("")}</select></label><button class="button">Добавить</button></form>` : ""}
  <form id="report-form" class="panel stack" hidden><label>Причина жалобы<textarea name="body" required maxlength="2000"></textarea></label><button class="button">Отправить жалобу</button></form>
  <h2>Комментарии</h2>
  <form id="comment-form" class="stack"><label>Ваш комментарий<textarea name="body" required maxlength="2000" placeholder="Напишите комментарий…"></textarea></label><button class="button primary">Отправить</button></form>
  <div id="comments">${comments.map(commentHTML).join("")}</div>${more(comments.length,50)}</section>`;
  let liked = v.liked;
  let likes = v.likes;
  const updateLike = () => { $("#like").innerHTML = icon("like") + " " + number(likes); $("#like").setAttribute("aria-pressed", liked); };
  updateLike();
  bind("#like", "click", async e => {
    if (!requireUser()) return;
    const button = e.currentTarget;
    button.disabled = true;
    try { await api(url + "/like", liked ? "DELETE" : "PUT"); likes += liked ? -1 : 1; liked = !liked; if (current()) updateLike(); }
    finally { button.disabled = false; }
  });
  subscription(v, current);
  const player = $("#player");
  let reportingView = false;
  async function reportView() {
    if (reportingView || !Number.isFinite(player.duration) || player.duration <= 0) return;
    let watched = 0;
    for (let i = 0; i < player.played.length; i++) {
      watched += player.played.end(i) - player.played.start(i);
    }
    if (watched < Math.min(3, player.duration / 2)) return;
    reportingView = true;
    try {
      const row = await api(url + "/views", "POST");
      if (current()) $("#views").textContent = number(row.views);
      if (row.accepted) player.removeEventListener("timeupdate", reportView);
      else reportingView = false;
    } catch {
      reportingView = false;
    }
  }
  player.addEventListener("timeupdate", reportView);
  bind("#save-video", "submit", async e => {
    e.preventDefault();
    const playlist = new FormData(e.target).get("playlist");
    await api("/api/playlists/" + playlist + "/videos/" + id, "PUT");
    toast("Видео добавлено в плейлист");
  });
  bind("#report-button", "click", () => { if (requireUser()) $("#report-form").hidden = !$("#report-form").hidden; });
  for (const [selector, resource] of [["#report-form","reports"],["#comment-form","comments"]]) {
    bind(selector, "submit", async e => {
      e.preventDefault();
      if (!requireUser()) return;
      const form = e.target;
      const body = new FormData(form).get("body");
      const button = form.querySelector("button");
      button.disabled = true;
      try {
        const row = await api(url + "/" + resource, "POST", {body});
        if (!current()) return;
        form.reset();
        if (resource === "reports") { form.hidden = true; toast("Жалоба отправлена"); }
        else {
          $("#comments").insertAdjacentHTML("afterbegin", commentHTML({...row, body, handle:user.handle, created_at:new Date()}));
          addedComments++;
        }
      } finally { button.disabled = false; }
    });
  }
  let addedComments = 0;
  pagination(comments.length, offset => api(url + "/comments?offset=" + (offset + addedComments)), rows =>
    $("#comments").insertAdjacentHTML("beforeend", rows.map(commentHTML).join("")), 50);
}

function subscription(channel, current) {
  const button = $("#subscribe");
  if (!button) return;
  const update = () => {
    button.textContent = channel.subscribed ? "Вы подписаны" : "Подписаться";
    button.setAttribute("aria-pressed", channel.subscribed);
    $("#subscribers").textContent = number(channel.subscribers) + " подписчиков";
  };
  update();
  bind("#subscribe", "click", async () => {
    if (!requireUser()) return;
    button.disabled = true;
    try {
      await api("/api/channels/" + channel.handle + "/subscription", channel.subscribed ? "DELETE" : "PUT");
      channel.subscribers += channel.subscribed ? -1 : 1;
      channel.subscribed = !channel.subscribed;
      if (current()) update();
    } finally { button.disabled = false; }
  });
}

async function channel(handle, query, current) {
  const url = "/api/channels/" + encodeURIComponent(handle);
  const c = await api(url);
  if (!current()) return;
  const playlists = query.get("tab") === "playlists";
  const own = user?.id === c.id;
  main.innerHTML = `<div class="channel-banner"></div><section class="channel-header">${avatar(c.handle,true)}
    <div><h1>@${esc(c.handle)}</h1><p id="subscribers">${number(c.subscribers)} подписчиков</p></div>
    ${own ? '<a data-nav class="button primary" href="/upload">Загрузить видео</a>' : '<button id="subscribe" class="button primary"></button>'}</section>
    <div class="pills"><a data-nav class="${playlists ? "" : "selected"}" href="${channelURL(handle)}">Видео</a>
    <a data-nav class="${playlists ? "selected" : ""}" href="${channelURL(handle)}?tab=playlists">Плейлисты</a></div>
    ${playlists ? `${own ? '<form id="new-playlist" class="inline"><label>Название плейлиста<input name="title" maxlength="120" required></label><button class="button primary">Создать</button></form>' : ""}
      ${c.playlists.length ? '<div class="playlist-grid">' + c.playlists.map(p => `<a class="panel" data-nav href="/playlist/${p.id}">${icon("library")} ${esc(p.title)}</a>`).join("") + '</div>' : empty("Плейлистов пока нет")}`
      : c.videos.length ? grid(c.videos) + more(c.videos.length) : empty("На канале пока нет видео")}`;
  subscription(c, current);
  bind("#new-playlist", "submit", async e => {
    e.preventDefault();
    await api("/api/playlists", "POST", Object.fromEntries(new FormData(e.target)));
    if (current()) render();
  });
  pagination(c.videos.length, async offset => (await api(url + "?offset=" + offset)).videos, rows =>
    $(".grid").insertAdjacentHTML("beforeend", rows.map(v => card(v)).join("")));
}

async function playlist(id, current) {
  const url = "/api/playlists/" + id;
  const p = await api(url);
  if (!current()) return;
  const own = user?.id === p.owner_id ? id : null;
  main.innerHTML = `<div class="page-heading"><div><h1>${esc(p.title)}</h1><p><a data-nav href="${channelURL(p.handle)}">@${esc(p.handle)}</a></p></div></div>
    ${p.videos.length ? grid(p.videos,false,own) + more(p.videos.length) : empty("В плейлисте пока нет доступных видео")}`;
  pagination(p.videos.length, async offset => (await api(url + "?offset=" + offset)).videos, rows =>
    $(".grid").insertAdjacentHTML("beforeend", rows.map(v => card(v,false,own)).join("")));
  main.onclick = async e => {
    const button = e.target.closest(".remove-video");
    if (!button || !own) return;
    button.disabled = true;
    try { await api(url + "/videos/" + button.dataset.id, "DELETE"); if (current()) render(); }
    catch (error) { toast(error.message); button.disabled = false; }
  };
}

async function manage(id, current) {
  if (!user) return signedOut("Войдите, чтобы управлять видео");
  const url = videoAPI(id);
  const owned = await api(url + "/access");
  const v = owned.video;
  const access = owned.users;
  if (!current()) return;
  main.innerHTML = `<div class="page-heading"><div><h1>Управление видео</h1><p>${esc(v.title)}${v.hidden ? " · Скрыто модератором" : ""}</p></div>
    <div class="inline">
      ${!v.hidden && v.status === "ready" ? `<a data-nav class="button" href="/watch/${id}">Смотреть</a>` : ""}
      <button type="button" id="delete-video" class="button danger">Удалить видео</button>
    </div></div>
    <div id="processing-status" class="panel access" role="status">${v.status === "ready" ? "Видео опубликовано" : "Получение статуса…"}</div>
    <button type="button" id="retry-video" class="button access" hidden>Повторить обработку</button>
    <form id="edit" class="panel stack form-wide">${metaFields(v)}<button class="button primary">Сохранить</button></form>
    <section class="panel form-wide access"><h2>Доступ к приватному видео</h2>
      <form id="grant" class="inline"><label>Имя пользователя<input name="handle" pattern="[A-Za-z0-9_]{3,30}" required></label><button class="button">Разрешить</button></form>
      <div id="grants">${access.map(a => `<div class="access-row"><span>@${esc(a.handle)}</span><button class="button revoke" data-handle="${esc(a.handle)}">Отозвать</button></div>`).join("")}</div>
    </section>`;
  async function pollStatus() {
    try {
      const status = await api(url + "/status");
      if (!current()) return;
      const panel = $("#processing-status");
      panel.hidden = false;
      panel.textContent = ({
        queued: status.error ? "Ожидается повторная обработка. " + status.error : "Видео ожидает обработки",
        processing: "Обработка видео — " + status.progress + "%",
        ready: "Видео опубликовано",
        failed: status.error || "Не удалось обработать видео"
      })[status.status];
      if (status.status === "ready") {
        toast("Видео опубликовано");
        render();
        return;
      }
      $("#retry-video").hidden = !status.can_retry;
    } catch (error) {
      if (!current() || error.status === 404 || error.status === 401) return;
    }
    setTimeout(() => { if (current()) pollStatus(); }, 1500);
  }
  if (v.status !== "ready") pollStatus();
  bind("#retry-video", "click", async e => {
    const button = e.currentTarget;
    button.disabled = true;
    try {
      await api(url + "/retry", "POST");
      if (current()) render();
    } finally {
      button.disabled = false;
    }
  });
  bind("#delete-video", "click", async e => {
    if (!confirm("Удалить видео и связанные комментарии? Это действие нельзя отменить.")) return;
    const button = e.currentTarget;
    button.disabled = true;
    try {
      await api(url, "DELETE");
      if (current()) {
        toast("Видео удалено");
        go("/mine", true);
      }
    } finally {
      button.disabled = false;
    }
  });
  bind("#edit", "submit", async e => {
    e.preventDefault();
    await api(url, "PATCH", metadata(e.target)); toast("Изменения сохранены"); if (current()) render();
  });
  bind("#grant", "submit", async e => {
    e.preventDefault();
    await api(url + "/access/" + encodeURIComponent(new FormData(e.target).get("handle")), "PUT");
    toast("Доступ выдан"); if (current()) render();
  });
  main.onclick = async e => {
    const button = e.target.closest(".revoke");
    if (!button) return;
    try { await api(url + "/access/" + encodeURIComponent(button.dataset.handle), "DELETE"); if (current()) render(); }
    catch (error) { toast(error.message); }
  };
}

async function moderation(query, current) {
  if (!user) return signedOut("Войдите, чтобы открыть панель модерации");
  if (user.role === "user") return empty("Недостаточно прав", "", "shield");
  const statuses = {open:"Новые", hidden:"Видео скрыто", dismissed:"Отклонённые"};
  const status = Object.hasOwn(statuses,query.get("status")) ? query.get("status") : "open";
  const fetchNext = offset => api("/api/moderation/reports?" + new URLSearchParams({status,offset}));
  const reports = await fetchNext(0);
  if (!current()) return;
  const reportHTML = r => `<article class="panel report"><div><h2>${r.title && !r.hidden ? `<a data-nav href="/watch/${r.video_id}">${esc(r.title)}</a>` : esc(r.title ?? "Приватное видео")}</h2>
    <small>@${esc(r.handle)} · ${date(r.created_at)}</small><p>${esc(r.reason)}</p></div>
    ${status === "open" ? `<div class="inline"><button class="button" data-action="dismiss" data-id="${r.id}">Отклонить</button>
      <button class="button danger" data-action="hide" data-id="${r.id}">Скрыть видео</button></div>` :
    r.hidden ? `<button class="button" data-action="restore" data-id="${r.video_id}">Восстановить</button>` : ""}</article>`;
  main.innerHTML = `<div class="page-heading"><h1>Модерация</h1></div>
    <div class="pills">${Object.entries(statuses).map(([key,label]) => `<a data-nav class="${status === key ? "selected" : ""}" href="/moderation?status=${key}">${label}</a>`).join("")}</div>
    <div id="reports">${reports.length ? reports.map(reportHTML).join("") : empty("Жалоб в этом разделе нет","","shield")}</div>${more(reports.length,50)}
    ${user.role === "admin" ? '<form id="roles" class="panel inline access"><label>Имя пользователя<input name="handle" pattern="[A-Za-z0-9_]{3,30}" required></label><label>Роль<select name="role"><option value="moderator">Модератор</option><option value="user">Пользователь</option></select></label><button class="button primary">Назначить</button></form>' : ""}`;
  pagination(reports.length, fetchNext, rows => $("#reports").insertAdjacentHTML("beforeend",rows.map(reportHTML).join("")), 50);
  main.onclick = async e => {
    const button = e.target.closest("[data-action]");
    if (!button) return;
    const {action,id} = button.dataset;
    button.disabled = true;
    try {
      if (action === "restore") await api("/api/moderation/videos/" + id, "PATCH", {hidden:false});
      else await api("/api/moderation/reports/" + id, "PATCH", {action});
      if (current()) render();
    } catch (error) { toast(error.message); button.disabled = false; }
  };
  bind("#roles", "submit", async e => {
    e.preventDefault();
    const {handle,role} = Object.fromEntries(new FormData(e.target));
    await api("/api/admin/accounts/" + encodeURIComponent(handle) + "/role", "PATCH", {role});
    toast("Роль обновлена");
  });
}

async function render() {
  const version = ++generation;
  const current = () => version === generation;
  const {pathname:path, search} = location;
  const query = new URLSearchParams(search);
  const [, section, id] = path.split("/");
  shell();
  main.onclick = null;
  main.innerHTML = '<div class="loading" role="status">Загрузка…</div>';
  try {
    let result;
    if (["/","/search","/subscriptions","/mine","/shared","/liked","/history"].includes(path)) result = await feed(path,query,current);
    else if (path === "/upload") result = upload();
    else if (path === "/login" || path === "/register") result = auth(path,query);
    else if (section === "watch" && /^[a-f0-9-]{36}$/.test(id)) result = await watch(id,current);
    else if (section === "manage" && /^[a-f0-9-]{36}$/.test(id)) result = await manage(id,current);
    else if (section === "channel" && /^[a-z0-9_]{3,30}$/i.test(id)) result = await channel(id,query,current);
    else if (section === "playlist" && /^\d+$/.test(id)) result = await playlist(id,current);
    else if (path === "/moderation") result = await moderation(query,current);
    else result = empty("Страница не найдена");
    if (current() && typeof result === "string") main.innerHTML = result;
  } catch (error) {
    if (current()) main.innerHTML = error.status === 401 ? signedOut("Требуется вход") : empty(esc(error.message));
  }
}
document.addEventListener("click", e => {
  const link = e.target.closest("a[data-nav]");
  if (!link || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  go(link.getAttribute("href"));
});
window.addEventListener("popstate", () => { if (pendingUpload) pendingUpload.abort(); render(); });
window.addEventListener("beforeunload", e => { if (pendingUpload) e.preventDefault(); });
api("/api/me").then(account => { user = account; }).catch(error => toast(error.message)).finally(render);

