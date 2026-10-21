(() => {
  const app = document.querySelector('[data-f-browser]');
  if (!app) return;
  const el = (id) => document.getElementById(id);
  const address = el('fb-address-input');
  const feedback = el('fb-feedback');
  const list = el('fb-record-list');
  const search = el('fb-record-search');
  let browserState = {};
  let tab = 'history';
  let entries = [];
  let busy = false;
  let alive = true;
  let refreshTimer = null;
  let frameTimer = null;
  let framePending = false;
  let previousFrame = null;
  let inputChain = Promise.resolve();
  const viewport = el('fb-viewport');
  const frameImage = el('fb-frame-image');
  const frameEmpty = el('fb-viewport-empty');
  const keyboard = el('fb-keyboard-input');

  function notice(message, isError = false) {
    if (!alive) return;
    feedback.hidden = !message;
    feedback.classList.toggle('is-error', isError);
    feedback.textContent = message || '';
  }

  async function json(url, method = 'GET', payload) {
    const response = await fetch(url, {
      method,
      credentials: 'same-origin',
      cache: 'no-store',
      headers: {
        Accept: 'application/json',
        ...(method !== 'GET' ? {'X-F-Browser-Action': '1'} : {}),
        ...(payload !== undefined ? {'Content-Type': 'application/json'} : {}),
      },
      ...(payload === undefined ? {} : {body: JSON.stringify(payload)}),
    });
    let data;
    try { data = await response.json(); } catch (_) { data = {}; }
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    return data;
  }

  const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function toggleBusy(working) {
    busy = working;
    app.querySelectorAll('[data-fb-command], [data-fb-action], #fb-reconnect, #fb-verify-gateway').forEach((button) => {
      button.disabled = working;
    });
  }

  async function task(url, payload) {
    if (busy) return;
    toggleBusy(true);
    notice('正在启动后台操作…');
    try {
      const result = await json(url, 'POST', payload);
      if (!result.task_id) throw new Error('主程序没有返回任务编号');
      for (let attempt = 0; attempt < 155 && alive; attempt += 1) {
        await pause(1200);
        const state = await json(`/api/tasks/${encodeURIComponent(result.task_id)}`);
        notice(state.message || '正在等待后台浏览器…');
        if (state.status === 'succeeded') {
          notice('操作完成');
          await Promise.all([refreshState(), refreshEntries()]);
          await refreshFrame();
          return;
        }
        if (state.status === 'failed' || state.status === 'cancelled') {
          throw new Error(state.error || state.message || '后台操作未完成');
        }
      }
      throw new Error('操作等待超时；可在任务中心查看详细状态');
    } catch (error) {
      notice(error.message || '浏览器操作失败', true);
      await refreshState();
    } finally {
      toggleBusy(false);
      await refreshState();
    }
  }

  async function refreshState() {
    if (!alive) return;
    try {
      const response = await json('/api/f-browser/state');
      const current = response.browser || {};
      const vpn = response.webvpn || {};
      browserState = current;
      const connected = vpn.state === 'authenticated';
      const checking = vpn.state === 'checking';
      el('fb-connection-dot').classList.toggle('is-connected', connected);
      el('fb-connection-text').textContent = vpn.reason || (checking ? '正在验证 WebVPN 网关' : 'WebVPN 状态未知');
      el('fb-browser-running').textContent = current.running ? '内嵌浏览器已运行' : '内嵌浏览器未启动';
      el('fb-page-title').textContent = current.title || (current.running ? '正在加载' : '尚未打开网页');
      el('fb-page-url').textContent = current.url || current.message || '';
      if (current.url && document.activeElement !== address) address.value = current.url;
      frameEmpty.hidden = Boolean(current.running && !frameImage.hidden);
      if (!busy) {
        const back = app.querySelector('[data-fb-command="back"]');
        const forward = app.querySelector('[data-fb-command="forward"]');
        if (back) back.disabled = !current.running || !current.can_back;
        if (forward) forward.disabled = !current.running || !current.can_forward;
      }
      if (current.recovery && !['idle', 'authenticated'].includes(current.recovery)) {
        notice(current.message || vpn.reason || 'WebVPN 正在恢复');
      }
    } catch (error) {
      el('fb-connection-text').textContent = '无法获取 WebVPN 状态';
    }
  }

  function recordButton(label, action, title) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn';
    button.textContent = label;
    button.title = title || label;
    button.addEventListener('click', action);
    return button;
  }

  function renderEntries() {
    list.replaceChildren();
    const term = search.value.trim().toLocaleLowerCase();
    const filtered = entries.filter((row) =>
      `${row.title || ''} ${row.url || ''}`.toLocaleLowerCase().includes(term));
    if (!filtered.length) {
      const empty = document.createElement('p');
      empty.className = 'fb-small';
      empty.textContent = tab === 'bookmarks' ? '还没有收藏的网站' : '没有匹配的浏览记录';
      list.append(empty);
      return;
    }
    for (const item of filtered) {
      const row = document.createElement('div');
      row.className = 'fb-record';
      const texts = document.createElement('div');
      texts.className = 'fb-record-text';
      const title = document.createElement('div');
      title.className = 'fb-record-title';
      title.textContent = item.title || '校园内网';
      const url = document.createElement('div');
      url.className = 'fb-record-url';
      url.title = item.url;
      url.textContent = item.url;
      texts.append(title, url);
      const actions = document.createElement('div');
      actions.className = 'fb-record-actions';
      actions.append(recordButton('打开', () => task('/api/f-browser/navigate', {url: item.url})));
      actions.append(recordButton('删除', async () => {
        try {
          const path = tab === 'history' ? 'history' : 'bookmarks';
          await json(`/api/f-browser/${path}/${item.id}`, 'DELETE');
          await refreshEntries();
        } catch (error) { notice(error.message, true); }
      }));
      row.append(texts, actions);
      list.append(row);
    }
  }

  async function refreshEntries() {
    if (!alive) return;
    try {
      const response = await json(`/api/f-browser/${tab}`);
      entries = response.items || [];
      renderEntries();
    } catch (error) { notice(error.message, true); }
  }

  app.querySelectorAll('[data-fb-command]').forEach((button) => {
    button.addEventListener('click', () =>
      task(`/api/f-browser/control/${encodeURIComponent(button.dataset.fbCommand)}`));
  });

  el('fb-address-form').addEventListener('submit', (event) => {
    event.preventDefault();
    task('/api/f-browser/navigate', {url: address.value.trim()});
  });

  el('fb-reconnect').addEventListener('click', () => {
    if (window.confirm('此操作会清除旧 WebVPN 会话并在内嵌页面重新认证。教务登录并不保证 WebVPN 已认证，建议优先使用「校验 WebVPN 网关」。确定重置吗？')) {
      task('/api/f-browser/reconnect');
    }
  });

  el('fb-bookmark-current').addEventListener('click', async () => {
    if (!browserState.url) { notice('请先访问可收藏的 WebVPN 网页', true); return; }
    try {
      await json('/api/f-browser/bookmarks', 'POST', {
        url: browserState.url,
        title: browserState.title || '校园内网',
      });
      notice('已加入收藏夹');
      if (tab === 'bookmarks') await refreshEntries();
    } catch (error) { notice(error.message, true); }
  });

  app.querySelectorAll('[data-fb-tab]').forEach((button) => {
    button.addEventListener('click', () => {
      tab = button.dataset.fbTab;
      app.querySelectorAll('[data-fb-tab]').forEach((item) => {
        const active = item.dataset.fbTab === tab;
        item.classList.toggle('is-active', active);
        item.setAttribute('aria-selected', String(active));
      });
      el('fb-clear-history').hidden = tab !== 'history';
      refreshEntries();
    });
  });

  el('fb-clear-history').addEventListener('click', async () => {
    if (!window.confirm('清空本机的 WebVPN 浏览历史？收藏夹不受影响。')) return;
    try {
      await json('/api/f-browser/history', 'DELETE');
      await refreshEntries();
    } catch (error) { notice(error.message, true); }
  });
  search.addEventListener('input', renderEntries);

  el('fb-verify-gateway').addEventListener('click', () => task('/api/f-browser/verify'));

  async function refreshFrame() {
    if (!alive || framePending || busy || !browserState.running) return;
    framePending = true;
    try {
      const response = await fetch('/api/f-browser/frame', {
        cache: 'no-store', credentials: 'same-origin',
        headers: {'X-F-Browser-Action': '1', Accept: 'image/jpeg'},
      });
      if (!response.ok) return;
      const blob = await response.blob();
      if (!alive) return;
      const url = URL.createObjectURL(blob);
      const old = previousFrame;
      frameImage.onload = () => {
        if (old) URL.revokeObjectURL(old);
        frameEmpty.hidden = true;
      };
      frameImage.src = url;
      frameImage.hidden = false;
      previousFrame = url;
    } catch (_) {
      // A frame can be unavailable while login/navigation is in progress.
    } finally {
      framePending = false;
    }
  }

  function queueInput(payload) {
    if (!alive || busy || !browserState.running) return;
    inputChain = inputChain.then(async () => {
      if (!alive || busy) return;
      await json('/api/f-browser/input', 'POST', payload);
    }).catch((error) => {
      notice('网页输入未完成：' + (error.message || '稍后重试'), true);
    });
  }

  viewport.addEventListener('pointerdown', (event) => {
    if (!browserState.running || frameImage.hidden) return;
    if (event.button !== 0) return;
    const rect = frameImage.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const x = (event.clientX - rect.left) / rect.width * 1280;
    const y = (event.clientY - rect.top) / rect.height * 760;
    if (x < 0 || y < 0 || x >= 1280 || y >= 760) return;
    event.preventDefault();
    queueInput({type: 'click', x, y});
    keyboard.focus({preventScroll: true});
  });

  viewport.addEventListener('wheel', (event) => {
    if (!browserState.running) return;
    event.preventDefault();
    queueInput({
      type: 'scroll',
      x: Math.max(-1200, Math.min(1200, event.deltaX)),
      y: Math.max(-1200, Math.min(1200, event.deltaY)),
    });
  }, {passive: false});

  keyboard.addEventListener('input', () => {
    if (!keyboard.value) return;
    const value = keyboard.value;
    keyboard.value = '';
    // Group text in a single Chromium keyboard.insert_text call, including IME.
    for (let offset = 0; offset < value.length; offset += 200) {
      queueInput({type: 'text', text: value.slice(offset, offset + 200)});
    }
  });

  keyboard.addEventListener('keydown', (event) => {
    const keyMap = {' ': 'Space'};
    const key = keyMap[event.key] || event.key;
    const keys = new Set(['Enter', 'Tab', 'Escape', 'Backspace', 'Delete',
      'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown',
      'Home', 'End', 'PageUp', 'PageDown', 'Space']);
    if (keys.has(key)) {
      event.preventDefault();
      queueInput({type: 'key', key});
    }
  });
  viewport.addEventListener('keydown', (event) => {
    if (event.target === viewport && browserState.running) keyboard.focus({preventScroll: true});
  });

  const cleanup = () => {
    alive = false;
    clearInterval(refreshTimer);
    clearInterval(frameTimer);
    if (previousFrame) URL.revokeObjectURL(previousFrame);
  };
  document.addEventListener('academic:page-before-swap', cleanup, {once: true});
  refreshState();
  refreshEntries();
  refreshTimer = setInterval(() => { if (!busy) refreshState(); }, 2500);
  frameTimer = setInterval(refreshFrame, 1100);
})();
