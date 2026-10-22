(() => {
  const root = document.querySelector('[data-f-browser]');
  if (!root) return;
  const byId = (id) => document.getElementById(id);
  const address = byId('fb-address-input');
  const feedback = byId('fb-feedback');
  const list = byId('fb-record-list');
  const search = byId('fb-record-search');
  const moreButton = byId('fb-more-toggle');
  const moreMenu = byId('fb-more-menu');
  let browser = {};
  let tab = 'history';
  let records = [];
  let busy = false;
  let alive = true;
  let stateTimer = null;
  let lastStatus = '';
  let lastUrl = '';

  const show = (message, error = false) => {
    if (!alive) return;
    feedback.hidden = !message;
    feedback.classList.toggle('is-error', error);
    feedback.textContent = message || '';
  };

  async function api(url, method = 'GET', data) {
    const response = await fetch(url, {
      method, cache: 'no-store', credentials: 'same-origin',
      headers: {
        Accept: 'application/json',
        ...(method !== 'GET' ? {'X-F-Browser-Action': '1'} : {}),
        ...(data === undefined ? {} : {'Content-Type': 'application/json'}),
      },
      ...(data === undefined ? {} : {body: JSON.stringify(data)}),
    });
    let value = {};
    try { value = await response.json(); } catch (_) {}
    if (!response.ok) throw new Error(value.detail || 'HTTP ' + response.status);
    return value;
  }

  const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function setBusy(value) {
    busy = value;
    root.querySelectorAll('[data-fb-command], [data-fb-action], #fb-reconnect, #fb-verify-gateway')
      .forEach((button) => { button.disabled = value; });
  }

  async function runTask(url, payload) {
    if (!alive || busy) return;
    setBusy(true);
    show('正在启动操作…');
    moreMenu.hidden = true;
    moreButton.setAttribute('aria-expanded', 'false');
    try {
      const started = await api(url, 'POST', payload);
      if (!started.task_id) throw new Error('主程序未返回任务编号');
      for (let i = 0; i < 130 && alive; i++) {
        await pause(1200);
        const result = await api('/api/tasks/' + encodeURIComponent(started.task_id));
        show(result.message || '正在操作浏览器…');
        if (result.status === 'succeeded') {
          show('');
          await Promise.all([refreshState(), refreshRecords()]);
          return;
        }
        if (result.status === 'failed' || result.status === 'cancelled') {
          throw new Error(result.error || result.message || '任务未成功');
        }
      }
      throw new Error('任务等待超时，请在任务中心查看状态');
    } catch (err) {
      show(err.message || '操作失败', true);
    } finally {
      setBusy(false);
      await refreshState();
    }
  }

  async function refreshState() {
    if (!alive) return;
    try {
      const result = await api('/api/f-browser/state');
      browser = result.browser || {};
      const vpn = result.webvpn || {};
      const authenticated = vpn.state === 'authenticated';
      byId('fb-connection-dot').classList.toggle('is-connected', authenticated);
      byId('fb-connection-text').textContent = vpn.reason || 'WebVPN 状态未知';
      byId('fb-browser-running').textContent = browser.running ? '窗口已打开' : '窗口未打开';
      byId('fb-page-title').textContent = browser.title || (browser.running ? '正在加载' : '尚未打开页面');
      byId('fb-page-url').textContent = browser.url || '网页将在独立 Chromium 窗口中打开';
      const labels = {
        ready: '已加载', login: '登录页面', loading: '加载中',
        error: '加载失败', closed: '未启动',
      };
      const pageStatus = browser.page_status || 'closed';
      byId('fb-page-status').textContent = labels[pageStatus] || pageStatus;
      if (browser.url && document.activeElement !== address) address.value = browser.url;
      if (!busy) {
        const back = root.querySelector('[data-fb-command="back"]');
        const forward = root.querySelector('[data-fb-command="forward"]');
        back.disabled = !browser.running || !browser.can_back;
        forward.disabled = !browser.running || !browser.can_forward;
      }
      if (pageStatus === 'error' && browser.message && browser.message !== lastStatus) {
        show(browser.message, true);
      }
      if ((browser.url && browser.url !== lastUrl) || pageStatus !== lastStatus) {
        lastUrl = browser.url || '';
        lastStatus = pageStatus;
        if (pageStatus === 'ready') refreshRecords();
      }
    } catch (_) {
      byId('fb-connection-text').textContent = '状态获取失败';
    }
  }

  function actionButton(label, callback) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn';
    button.textContent = label;
    button.addEventListener('click', callback);
    return button;
  }

  function renderRecords() {
    list.replaceChildren();
    const q = search.value.trim().toLowerCase();
    const visible = records.filter((record) =>
      (record.title + ' ' + record.url).toLowerCase().includes(q));
    if (!visible.length) {
      const empty = document.createElement('p');
      empty.className = 'fb-meta';
      empty.textContent = tab === 'history' ? '没有浏览记录' : '还没有收藏';
      list.append(empty);
      return;
    }
    for (const record of visible) {
      const row = document.createElement('div');
      row.className = 'fb-record';
      const text = document.createElement('div');
      text.className = 'fb-record-text';
      const title = document.createElement('strong');
      title.textContent = record.title || '校园网页';
      const url = document.createElement('span');
      url.textContent = record.url;
      url.title = record.url;
      text.append(title, url);
      row.append(text);
      row.append(actionButton('打开', () =>
        runTask('/api/f-browser/navigate', {url: record.url})));
      row.append(actionButton('删除', async () => {
        try {
          await api('/api/f-browser/' + tab + '/' + record.id, 'DELETE');
          await refreshRecords();
        } catch (error) { show(error.message, true); }
      }));
      list.append(row);
    }
  }

  async function refreshRecords() {
    if (!alive) return;
    try {
      const result = await api('/api/f-browser/' + tab);
      records = result.items || [];
      renderRecords();
    } catch (error) { show(error.message, true); }
  }

  root.querySelectorAll('[data-fb-command]').forEach((button) => {
    button.addEventListener('click', () =>
      runTask('/api/f-browser/control/' + encodeURIComponent(button.dataset.fbCommand)));
  });
  byId('fb-address-form').addEventListener('submit', (event) => {
    event.preventDefault();
    runTask('/api/f-browser/navigate', {url: address.value.trim()});
  });
  byId('fb-reconnect').addEventListener('click', () => {
    if (window.confirm('让主程序重新建立 WebVPN 会话？可能需要学校短信或验证码验证。')) {
      runTask('/api/f-browser/reconnect');
    }
  });
  byId('fb-verify-gateway').addEventListener('click', () =>
    runTask('/api/f-browser/verify'));
  byId('fb-bookmark-current').addEventListener('click', async () => {
    if (!browser.url || !browser.running) {
      show('请先打开可收藏的校园网页', true);
      return;
    }
    try {
      await api('/api/f-browser/bookmarks', 'POST', {
        url: browser.url, title: browser.title || '校园网页',
      });
      show('已收藏当前页面');
      if (tab === 'bookmarks') await refreshRecords();
    } catch (error) { show(error.message, true); }
  });

  moreButton.addEventListener('click', () => {
    moreMenu.hidden = !moreMenu.hidden;
    moreButton.setAttribute('aria-expanded', String(!moreMenu.hidden));
  });
  document.addEventListener('click', (event) => {
    if (!event.target.closest('.fb-more')) {
      moreMenu.hidden = true;
      moreButton.setAttribute('aria-expanded', 'false');
    }
  });

  root.querySelectorAll('[data-fb-tab]').forEach((button) => {
    button.addEventListener('click', () => {
      tab = button.dataset.fbTab;
      root.querySelectorAll('[data-fb-tab]').forEach((item) => {
        const selected = item.dataset.fbTab === tab;
        item.classList.toggle('is-active', selected);
        item.setAttribute('aria-selected', String(selected));
      });
      byId('fb-clear-history').hidden = tab !== 'history';
      refreshRecords();
    });
  });
  byId('fb-clear-history').addEventListener('click', async () => {
    if (!window.confirm('清空本机浏览历史？收藏不会删除。')) return;
    try {
      await api('/api/f-browser/history', 'DELETE');
      await refreshRecords();
    } catch (error) { show(error.message, true); }
  });
  search.addEventListener('input', renderRecords);

  const cleanup = () => {
    alive = false;
    clearInterval(stateTimer);
  };
  document.addEventListener('academic:page-before-swap', cleanup, {once: true});
  refreshState();
  refreshRecords();
  // Cheap status snapshot polling only; web events and metadata are tracked
  // inside Chromium, no frame or keystroke is streamed to this page.
  stateTimer = setInterval(() => { if (!busy) refreshState(); }, 3000);
})();
