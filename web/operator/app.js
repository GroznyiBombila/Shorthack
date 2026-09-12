// Консоль оператора поддержки.
// window.USE_MOCK = true подменяет все запросы к API заготовленными данными —
// удобно для демонстрации без бэкенда. По умолчанию выключено.
window.USE_MOCK = true;

(function () {
  'use strict';

  var TOKEN_KEY = 'operator_token';
  var POLL_INTERVAL_MS = 20000;

  var STATUS_LABELS = {
    new: 'Новое',
    in_progress: 'В работе',
    answered: 'Отвечено',
    closed: 'Закрыто'
  };

  var PRIORITY_LABELS = {
    P1: 'P1 · критично',
    P2: 'P2 · высокий',
    P3: 'P3 · обычный',
    P4: 'P4 · низкий'
  };

  var TAB_ORDER = ['new', 'in_progress', 'answered', 'all'];

  // ---------------------------------------------------------------------
  // Состояние
  // ---------------------------------------------------------------------

  var state = {
    token: null,
    tickets: [],          // последний успешно загруженный полный список (status=all)
    selectedId: null,
    selectedTicket: null, // полная карточка выбранного обращения
    activeTab: 'new',
    autoRefresh: true,
    pollTimer: null,
    listLoading: false,
    listInitialError: null, // текст ошибки, если первую загрузку так и не удалось выполнить
    connLost: false,         // фоновое обновление не удалось, но старые данные ещё показаны
    cardLoading: false,
    cardError: null,
    sending: false,
    changingStatus: false
  };

  // ---------------------------------------------------------------------
  // DOM
  // ---------------------------------------------------------------------

  var dom = {};

  function cacheDom() {
    dom.loginScreen = document.getElementById('loginScreen');
    dom.mainScreen = document.getElementById('mainScreen');
    dom.tokenInput = document.getElementById('tokenInput');
    dom.loginBtn = document.getElementById('loginBtn');
    dom.loginError = document.getElementById('loginError');
    dom.logoutBtn = document.getElementById('logoutBtn');
    dom.autoRefreshToggle = document.getElementById('autoRefreshToggle');
    dom.connStatus = document.getElementById('connStatus');
    dom.statusTabs = document.getElementById('statusTabs');
    dom.queueList = document.getElementById('queueList');
    dom.ticketPane = document.getElementById('ticketPane');
  }

  // ---------------------------------------------------------------------
  // Утилиты
  // ---------------------------------------------------------------------

  function escapeHtml(value) {
    var div = document.createElement('div');
    div.textContent = value == null ? '' : String(value);
    return div.innerHTML;
  }

  function formatDateTime(iso) {
    if (!iso) return '—';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '—';
    var pad = function (n) { return n < 10 ? '0' + n : '' + n; };
    return pad(d.getDate()) + '.' + pad(d.getMonth() + 1) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  function computeCounts(list) {
    var counts = { new: 0, in_progress: 0, answered: 0, closed: 0, all: 0 };
    for (var i = 0; i < list.length; i++) {
      var s = list[i].status;
      if (counts.hasOwnProperty(s)) counts[s]++;
      counts.all++;
    }
    return counts;
  }

  function filteredTickets() {
    if (state.activeTab === 'all') return state.tickets.slice();
    return state.tickets.filter(function (t) { return t.status === state.activeTab; });
  }

  // ---------------------------------------------------------------------
  // API (реальное и демо)
  // ---------------------------------------------------------------------

  function apiRequest(path, options) {
    options = options || {};
    if (window.USE_MOCK) {
      return mockRequest(path, options);
    }

    var headers = { 'X-Operator-Token': state.token || '' };
    if (options.body) headers['Content-Type'] = 'application/json';
    if (options.headers) {
      for (var k in options.headers) headers[k] = options.headers[k];
    }

    return fetch(path, {
      method: options.method || 'GET',
      headers: headers,
      body: options.body
    }).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        if (text) {
          try { data = JSON.parse(text); } catch (e) { data = null; }
        }
        if (!res.ok) {
          var code = data && data.error && data.error.code;
          var message = (data && data.error && data.error.message) || ('Ошибка сервера (' + res.status + ')');
          var err = new Error(message);
          err.kind = 'api';
          err.status = res.status;
          err.code = code;
          throw err;
        }
        return data || {};
      });
    }, function () {
      var err = new Error('Нет соединения с сервером');
      err.kind = 'network';
      throw err;
    });
  }

  function fetchTicketsAll() {
    return apiRequest('/api/operator/tickets?status=all&limit=50');
  }

  function fetchTicketDetail(id) {
    return apiRequest('/api/operator/tickets/' + encodeURIComponent(id));
  }

  function sendReply(id, text) {
    return apiRequest('/api/operator/tickets/' + encodeURIComponent(id) + '/reply', {
      method: 'POST',
      body: JSON.stringify({ text: text })
    });
  }

  function sendStatus(id, status) {
    return apiRequest('/api/operator/tickets/' + encodeURIComponent(id) + '/status', {
      method: 'POST',
      body: JSON.stringify({ status: status })
    });
  }

  // ---------------------------------------------------------------------
  // Демо-данные (USE_MOCK)
  // ---------------------------------------------------------------------

  var mockDb = null;

  function hoursAgoIso(h) {
    return new Date(Date.now() - h * 3600 * 1000).toISOString();
  }

  function buildMockDb() {
    var tickets = [
      {
        ticket_id: 'TCK-1001',
        subject: 'Не могу восстановить пароль от личного кабинета',
        status: 'new',
        priority: 'P1',
        category: 'Аккаунт',
        recipient: 'Деканат',
        anonymous: false,
        email: 'ivanov@stud.example.edu',
        created_at: hoursAgoIso(1),
        last_message_at: hoursAgoIso(1),
        body: 'Здравствуйте! Пытаюсь восстановить пароль третий раз — письмо со ссылкой не приходит ни на почту, ни в спам. Нужно сдать курсовую сегодня, помогите, пожалуйста, срочно.',
        messages: [
          { author: 'user', text: 'Здравствуйте! Пытаюсь восстановить пароль третий раз — письмо со ссылкой не приходит ни на почту, ни в спам. Нужно сдать курсовую сегодня, помогите, пожалуйста, срочно.', created_at: hoursAgoIso(1) }
        ]
      },
      {
        ticket_id: 'TCK-1002',
        subject: 'Ошибка при загрузке курсовой работы',
        status: 'new',
        priority: 'P2',
        category: 'Учебный портал',
        recipient: 'Кафедра информатики',
        anonymous: true,
        email: null,
        created_at: hoursAgoIso(3),
        last_message_at: hoursAgoIso(3),
        body: 'Портал выдаёт "Ошибка 413" при попытке загрузить файл курсовой (45 МБ). Дедлайн завтра утром, подскажите, как быть.',
        messages: [
          { author: 'user', text: 'Портал выдаёт "Ошибка 413" при попытке загрузить файл курсовой (45 МБ). Дедлайн завтра утром, подскажите, как быть.', created_at: hoursAgoIso(3) }
        ]
      },
      {
        ticket_id: 'TCK-1003',
        subject: 'Когда откроется запись на пересдачу?',
        status: 'in_progress',
        priority: 'P3',
        category: 'Расписание',
        recipient: 'Деканат',
        anonymous: false,
        email: 'petrova@stud.example.edu',
        created_at: hoursAgoIso(30),
        last_message_at: hoursAgoIso(5),
        body: 'Подскажите, пожалуйста, когда откроется запись на пересдачу по матанализу?',
        messages: [
          { author: 'user', text: 'Подскажите, пожалуйста, когда откроется запись на пересдачу по матанализу?', created_at: hoursAgoIso(30) },
          { author: 'operator', text: 'Добрый день! Уточняю у деканата точную дату, вернусь с ответом в течение дня.', created_at: hoursAgoIso(5) }
        ]
      },
      {
        ticket_id: 'TCK-1004',
        subject: 'Не приходит стипендия второй месяц',
        status: 'in_progress',
        priority: 'P1',
        category: 'Финансы',
        recipient: 'Бухгалтерия',
        anonymous: false,
        email: 'sidorov@stud.example.edu',
        created_at: hoursAgoIso(50),
        last_message_at: hoursAgoIso(2),
        body: 'Стипендия не приходит уже второй месяц подряд, хотя академической задолженности нет. Реквизиты не менялись.',
        messages: [
          { author: 'user', text: 'Стипендия не приходит уже второй месяц подряд, хотя академической задолженности нет. Реквизиты не менялись.', created_at: hoursAgoIso(50) },
          { author: 'operator', text: 'Передал запрос в бухгалтерию, ожидаю ответ по вашему делу.', created_at: hoursAgoIso(20) },
          { author: 'user', text: 'Спасибо, буду ждать. Есть какие-то сроки?', created_at: hoursAgoIso(2) }
        ]
      },
      {
        ticket_id: 'TCK-1005',
        subject: 'Спасибо, вопрос решён',
        status: 'answered',
        priority: 'P4',
        category: 'Общие вопросы',
        recipient: 'Служба поддержки',
        anonymous: false,
        email: 'nikitina@stud.example.edu',
        created_at: hoursAgoIso(80),
        last_message_at: hoursAgoIso(40),
        body: 'Подскажите режим работы читального зала в сессию.',
        messages: [
          { author: 'user', text: 'Подскажите режим работы читального зала в сессию.', created_at: hoursAgoIso(80) },
          { author: 'operator', text: 'В сессию читальный зал работает с 8:00 до 22:00 без выходных.', created_at: hoursAgoIso(40) }
        ]
      },
      {
        ticket_id: 'TCK-1006',
        subject: 'Жалоба на работу wifi в общежитии',
        status: 'answered',
        priority: 'P2',
        category: 'Инфраструктура',
        recipient: 'Общежитие №3',
        anonymous: true,
        email: null,
        created_at: hoursAgoIso(120),
        last_message_at: hoursAgoIso(60),
        body: 'Уже неделю в общежитии №3 на 4 этаже не работает wifi, невозможно заниматься онлайн.',
        messages: [
          { author: 'user', text: 'Уже неделю в общежитии №3 на 4 этаже не работает wifi, невозможно заниматься онлайн.', created_at: hoursAgoIso(120) },
          { author: 'operator', text: 'Заявка передана техническому отделу общежития, обещали устранить в течение трёх дней.', created_at: hoursAgoIso(60) }
        ]
      },
      {
        ticket_id: 'TCK-1007',
        subject: 'Ошибка 500 при подаче заявления на перевод',
        status: 'closed',
        priority: 'P3',
        category: 'Учебный портал',
        recipient: 'Деканат',
        anonymous: false,
        email: 'orlov@stud.example.edu',
        created_at: hoursAgoIso(200),
        last_message_at: hoursAgoIso(150),
        body: 'При подаче заявления на перевод на другую программу портал выдаёт ошибку 500.',
        messages: [
          { author: 'user', text: 'При подаче заявления на перевод на другую программу портал выдаёт ошибку 500.', created_at: hoursAgoIso(200) },
          { author: 'operator', text: 'Проблема была на стороне портала, уже исправлена. Попробуйте подать заявление ещё раз.', created_at: hoursAgoIso(170) },
          { author: 'user', text: 'Получилось, заявление подано. Спасибо!', created_at: hoursAgoIso(160) },
          { author: 'operator', text: 'Отлично, рады помочь. Закрываю обращение.', created_at: hoursAgoIso(150) }
        ]
      }
    ];
    return { tickets: tickets, validToken: 'demo-token' };
  }

  function summaryOf(t) {
    return {
      ticket_id: t.ticket_id,
      subject: t.subject,
      status: t.status,
      priority: t.priority,
      category: t.category,
      recipient: t.recipient,
      anonymous: t.anonymous,
      email: t.email,
      created_at: t.created_at,
      last_message_at: t.last_message_at,
      messages_count: t.messages.length
    };
  }

  function mockRequest(path, options) {
    if (!mockDb) mockDb = buildMockDb();

    return new Promise(function (resolve, reject) {
      setTimeout(function () {
        try {
          resolve(mockHandle(path, options));
        } catch (err) {
          reject(err);
        }
      }, 220);
    });
  }

  function mockAuthError() {
    var err = new Error('Неверный токен сотрудника');
    err.kind = 'api';
    err.status = 401;
    err.code = 'unauthorized';
    return err;
  }

  function mockHandle(path, options) {
    // В демо-режиме принимаем любой непустой токен, кроме заведомо неверного маркера,
    // чтобы можно было показать и успешный вход, и ошибку 401.
    if (state.token === 'wrong') throw mockAuthError();

    var m;

    m = path.match(/^\/api\/operator\/tickets\?status=([^&]+)&limit=(\d+)$/);
    if (m) {
      var status = decodeURIComponent(m[1]);
      var limit = parseInt(m[2], 10);
      var list = mockDb.tickets;
      if (status !== 'all') list = list.filter(function (t) { return t.status === status; });
      return { tickets: list.slice(0, limit).map(summaryOf) };
    }

    m = path.match(/^\/api\/operator\/tickets\/([^/]+)$/);
    if (m && (!options.method || options.method === 'GET')) {
      var t1 = findMockTicket(m[1]);
      var full = summaryOf(t1);
      full.body = t1.body;
      full.messages = t1.messages;
      return full;
    }

    m = path.match(/^\/api\/operator\/tickets\/([^/]+)\/reply$/);
    if (m && options.method === 'POST') {
      var t2 = findMockTicket(m[1]);
      var payload = JSON.parse(options.body || '{}');
      t2.messages.push({ author: 'operator', text: payload.text, created_at: new Date().toISOString() });
      t2.last_message_at = new Date().toISOString();
      var full2 = summaryOf(t2);
      full2.body = t2.body;
      full2.messages = t2.messages;
      return full2;
    }

    m = path.match(/^\/api\/operator\/tickets\/([^/]+)\/status$/);
    if (m && options.method === 'POST') {
      var t3 = findMockTicket(m[1]);
      var payload2 = JSON.parse(options.body || '{}');
      t3.status = payload2.status;
      var full3 = summaryOf(t3);
      full3.body = t3.body;
      full3.messages = t3.messages;
      return full3;
    }

    var notFound = new Error('Обращение не найдено');
    notFound.kind = 'api';
    notFound.status = 404;
    throw notFound;
  }

  function findMockTicket(id) {
    for (var i = 0; i < mockDb.tickets.length; i++) {
      if (mockDb.tickets[i].ticket_id === id) return mockDb.tickets[i];
    }
    var err = new Error('Обращение не найдено');
    err.kind = 'api';
    err.status = 404;
    throw err;
  }

  // ---------------------------------------------------------------------
  // Экран входа
  // ---------------------------------------------------------------------

  function showLoginError(message) {
    dom.loginError.textContent = message;
    dom.loginError.hidden = false;
  }

  function hideLoginError() {
    dom.loginError.hidden = true;
  }

  function setLoginBusy(busy) {
    dom.loginBtn.disabled = busy;
    dom.loginBtn.textContent = busy ? 'Проверяю…' : 'Войти';
  }

  function attemptLogin(token, options) {
    options = options || {};
    hideLoginError();
    setLoginBusy(true);
    state.token = token;

    fetchTicketsAll().then(function () {
      setLoginBusy(false);
      try { localStorage.setItem(TOKEN_KEY, token); } catch (e) { /* демо/приватный режим браузера */ }
      showMainScreen();
    }, function (err) {
      setLoginBusy(false);
      if (err.status === 401 || err.status === 403) {
        state.token = null;
        try { localStorage.removeItem(TOKEN_KEY); } catch (e) {}
        showLoginError('Неверный токен сотрудника.');
      } else if (err.status === 503 && err.code === 'not_configured') {
        state.token = null;
        showLoginError('Токен на сервере не настроен. Обратитесь к администратору.');
      } else if (!options.silent) {
        showLoginError('Не удалось соединиться с сервером. Попробуйте ещё раз.');
      } else {
        // тихая попытка входа по сохранённому токену — просто остаёмся на экране входа
        state.token = null;
      }
    });
  }

  function handleLoginClick() {
    var token = dom.tokenInput.value.trim();
    if (!token) {
      showLoginError('Введите токен.');
      return;
    }
    attemptLogin(token);
  }

  function handleLogout() {
    stopPolling();
    state.token = null;
    state.tickets = [];
    state.selectedId = null;
    state.selectedTicket = null;
    state.listInitialError = null;
    state.connLost = false;
    try { localStorage.removeItem(TOKEN_KEY); } catch (e) {}
    dom.tokenInput.value = '';
    hideLoginError();
    dom.loginScreen.hidden = false;
    dom.mainScreen.hidden = true;
  }

  // ---------------------------------------------------------------------
  // Основной экран — очередь
  // ---------------------------------------------------------------------

  function showMainScreen() {
    dom.loginScreen.hidden = true;
    dom.mainScreen.hidden = false;
    refreshQueue({ initial: true });
    startPolling();
  }

  function refreshQueue(opts) {
    opts = opts || {};
    if (opts.initial) {
      state.listLoading = true;
      state.listInitialError = null;
      renderQueue();
    }

    return fetchTicketsAll().then(function (data) {
      state.tickets = (data && data.tickets) || [];
      state.listLoading = false;
      state.listInitialError = null;
      state.connLost = false;
      renderQueue();
      if (state.selectedId) refreshCardFromList();
    }, function (err) {
      state.listLoading = false;
      if (opts.initial || state.tickets.length === 0) {
        state.listInitialError = describeError(err);
        renderQueue();
      } else {
        // Данные на экране уже есть — не перекрываем их ошибкой, просто помечаем разрыв связи.
        state.connLost = true;
        renderConnStatus();
      }
      if (err.status === 401 || err.status === 403) {
        handleLogout();
        showLoginError('Неверный токен сотрудника.');
      }
    });
  }

  function refreshCardFromList() {
    // Обновляем счётчик сообщений/время в открытой карточке, если она есть в свежем списке.
    var found = null;
    for (var i = 0; i < state.tickets.length; i++) {
      if (state.tickets[i].ticket_id === state.selectedId) { found = state.tickets[i]; break; }
    }
    if (!found) return;
    if (state.selectedTicket && state.selectedTicket.messages_count !== found.messages_count) {
      // Новые сообщения появились фоном — подтягиваем полную карточку.
      loadTicketDetail(state.selectedId, { silent: true });
    }
  }

  function describeError(err) {
    if (err && err.kind === 'network') return 'Нет соединения с сервером.';
    if (err && err.status === 503 && err.code === 'not_configured') return 'Токен на сервере не настроен.';
    if (err && err.message) return err.message;
    return 'Не удалось загрузить очередь.';
  }

  function renderConnStatus() {
    dom.connStatus.hidden = !state.connLost;
  }

  function renderTabs() {
    var counts = computeCounts(state.tickets);
    var buttons = dom.statusTabs.querySelectorAll('.tab');
    for (var i = 0; i < buttons.length; i++) {
      var btn = buttons[i];
      var status = btn.getAttribute('data-status');
      btn.classList.toggle('active', status === state.activeTab);
      var countEl = btn.querySelector('.count');
      if (countEl) countEl.textContent = String(counts[status] || 0);
    }
  }

  function renderQueue() {
    renderTabs();
    renderConnStatus();

    if (state.listLoading) {
      dom.queueList.innerHTML = '<div class="queue-state">Загрузка обращений…</div>';
      return;
    }

    if (state.listInitialError) {
      dom.queueList.innerHTML =
        '<div class="queue-state">' +
        escapeHtml(state.listInitialError) +
        '<div><button type="button" class="btn-secondary" id="retryQueueBtn">Повторить</button></div>' +
        '</div>';
      var retryBtn = document.getElementById('retryQueueBtn');
      if (retryBtn) retryBtn.addEventListener('click', function () { refreshQueue({ initial: true }); });
      return;
    }

    var list = filteredTickets();

    if (list.length === 0) {
      dom.queueList.innerHTML = '<div class="queue-state">Обращений нет — это нормально.</div>';
      return;
    }

    var html = list.map(renderTicketRowHtml).join('');
    dom.queueList.innerHTML = html;

    var rows = dom.queueList.querySelectorAll('.ticket-row');
    for (var i = 0; i < rows.length; i++) {
      rows[i].addEventListener('click', onTicketRowClick);
    }
  }

  function renderTicketRowHtml(t) {
    var classes = ['ticket-row'];
    if (t.status === 'new') classes.push('is-new');
    if (t.ticket_id === state.selectedId) classes.push('is-selected');

    return (
      '<button type="button" class="' + classes.join(' ') + '" data-id="' + escapeHtml(t.ticket_id) + '">' +
        '<div class="ticket-row-top">' +
          '<span class="ticket-id">' + escapeHtml(t.ticket_id) + '</span>' +
          '<span class="priority-chip priority-' + escapeHtml(t.priority) + '">' + escapeHtml(t.priority) + '</span>' +
          (t.anonymous ? '<span class="anon-chip">аноним</span>' : '') +
        '</div>' +
        '<div class="ticket-subject">' + escapeHtml(t.subject) + '</div>' +
        '<div class="ticket-row-bottom">' +
          '<span class="ticket-recipient">' + escapeHtml(t.recipient || '—') + '</span>' +
          '<span class="ticket-time">' + escapeHtml(formatDateTime(t.last_message_at || t.created_at)) + '</span>' +
        '</div>' +
      '</button>'
    );
  }

  function onTicketRowClick(ev) {
    var id = ev.currentTarget.getAttribute('data-id');
    selectTicket(id);
  }

  function onTabClick(ev) {
    var btn = ev.target.closest ? ev.target.closest('.tab') : null;
    if (!btn) return;
    var status = btn.getAttribute('data-status');
    if (!status || status === state.activeTab) return;
    state.activeTab = status;
    renderQueue();
  }

  // ---------------------------------------------------------------------
  // Карточка обращения
  // ---------------------------------------------------------------------

  function selectTicket(id) {
    state.selectedId = id;
    state.selectedTicket = null;
    state.cardError = null;
    renderQueue(); // подсветить выбранную строку
    loadTicketDetail(id);
  }

  function loadTicketDetail(id, opts) {
    opts = opts || {};
    if (!opts.silent) {
      state.cardLoading = true;
      state.cardError = null;
      renderTicketCard();
    }

    fetchTicketDetail(id).then(function (data) {
      if (state.selectedId !== id) return; // выбор успели сменить
      state.cardLoading = false;
      state.cardError = null;
      state.selectedTicket = data;
      renderTicketCard();
    }, function (err) {
      if (state.selectedId !== id) return;
      state.cardLoading = false;
      if (err.status === 401 || err.status === 403) {
        handleLogout();
        showLoginError('Неверный токен сотрудника.');
        return;
      }
      state.cardError = describeError(err);
      renderTicketCard();
    });
  }

  function renderTicketCard() {
    if (!state.selectedId) {
      dom.ticketPane.innerHTML = '<div class="placeholder">Выберите обращение слева, чтобы увидеть подробности.</div>';
      return;
    }

    if (state.cardLoading) {
      dom.ticketPane.innerHTML = '<div class="placeholder">Загрузка обращения…</div>';
      return;
    }

    if (state.cardError) {
      dom.ticketPane.innerHTML =
        '<div class="card-error">' + escapeHtml(state.cardError) + '</div>' +
        '<button type="button" class="btn-secondary" id="retryCardBtn">Повторить</button>';
      var retryBtn = document.getElementById('retryCardBtn');
      if (retryBtn) retryBtn.addEventListener('click', function () { loadTicketDetail(state.selectedId); });
      return;
    }

    var t = state.selectedTicket;
    if (!t) return;

    var requester = t.anonymous ? 'Аноним' : (t.email || '—');

    var statusOptions = ['new', 'in_progress', 'answered', 'closed'].map(function (s) {
      var selected = s === t.status ? ' selected' : '';
      return '<option value="' + s + '"' + selected + '>' + STATUS_LABELS[s] + '</option>';
    }).join('');

    var messagesHtml = (t.messages || []).map(function (m) {
      var cls = m.author === 'operator' ? 'message-operator' : 'message-user';
      var who = m.author === 'operator' ? 'Сотрудник' : 'Студент';
      return (
        '<div class="message ' + cls + '">' +
          '<div class="message-meta"><span>' + escapeHtml(who) + '</span><span>' + escapeHtml(formatDateTime(m.created_at)) + '</span></div>' +
          '<div class="message-text">' + escapeHtml(m.text) + '</div>' +
        '</div>'
      );
    }).join('');

    if (!messagesHtml) {
      messagesHtml = '<div class="placeholder" style="margin-top:0;">Переписки пока нет.</div>';
    }

    dom.ticketPane.innerHTML =
      '<div class="ticket-card-header">' +
        '<h2>' + escapeHtml(t.ticket_id) + ' — ' + escapeHtml(t.subject) + '</h2>' +
        '<div class="status-control">' +
          '<span class="status-badge status-' + escapeHtml(t.status) + '">' + STATUS_LABELS[t.status] + '</span>' +
          '<select id="statusSelect">' + statusOptions + '</select>' +
        '</div>' +
      '</div>' +
      '<div class="ticket-meta">' +
        '<div><div class="meta-label">Категория</div><div class="meta-value">' + escapeHtml(t.category || '—') + '</div></div>' +
        '<div><div class="meta-label">Приоритет</div><div class="meta-value">' + escapeHtml(PRIORITY_LABELS[t.priority] || t.priority) + '</div></div>' +
        '<div><div class="meta-label">Адресат</div><div class="meta-value">' + escapeHtml(t.recipient || '—') + '</div></div>' +
        '<div><div class="meta-label">Заявитель</div><div class="meta-value">' + escapeHtml(requester) + '</div></div>' +
      '</div>' +
      '<div class="ticket-body">' + escapeHtml(t.body || '') + '</div>' +
      '<div class="messages">' + messagesHtml + '</div>' +
      '<div class="reply-box">' +
        '<textarea id="replyText" placeholder="Введите ответ студенту…"></textarea>' +
        '<div class="reply-actions">' +
          '<button type="button" class="btn-primary" id="replyBtn">Ответить</button>' +
        '</div>' +
      '</div>';

    var statusSelect = document.getElementById('statusSelect');
    if (statusSelect) statusSelect.addEventListener('change', onStatusChange);

    var replyBtn = document.getElementById('replyBtn');
    if (replyBtn) replyBtn.addEventListener('click', onReplyClick);

    if (state.sending && replyBtn) {
      replyBtn.disabled = true;
      replyBtn.textContent = 'Отправляю…';
    }
    if (state.changingStatus && statusSelect) {
      statusSelect.disabled = true;
    }
  }

  function onReplyClick() {
    var textarea = document.getElementById('replyText');
    var text = textarea ? textarea.value.trim() : '';
    if (!text) {
      textarea && textarea.focus();
      return;
    }
    if (state.sending) return;

    state.sending = true;
    var replyBtn = document.getElementById('replyBtn');
    if (replyBtn) {
      replyBtn.disabled = true;
      replyBtn.textContent = 'Отправляю…';
    }

    var id = state.selectedId;
    sendReply(id, text).then(function () {
      state.sending = false;
      // Забираем полную карточку заново, чтобы гарантированно получить актуальную переписку.
      loadTicketDetail(id, { silent: true });
      refreshQueue();
    }, function (err) {
      state.sending = false;
      if (err.status === 401 || err.status === 403) {
        handleLogout();
        showLoginError('Неверный токен сотрудника.');
        return;
      }
      window.alert('Не удалось отправить ответ: ' + describeError(err));
      renderTicketCard();
    });
  }

  function onStatusChange(ev) {
    var newStatus = ev.target.value;
    var id = state.selectedId;
    state.changingStatus = true;
    ev.target.disabled = true;

    sendStatus(id, newStatus).then(function () {
      state.changingStatus = false;
      loadTicketDetail(id, { silent: true });
      refreshQueue();
    }, function (err) {
      state.changingStatus = false;
      if (err.status === 401 || err.status === 403) {
        handleLogout();
        showLoginError('Неверный токен сотрудника.');
        return;
      }
      window.alert('Не удалось изменить статус: ' + describeError(err));
      renderTicketCard();
    });
  }

  // ---------------------------------------------------------------------
  // Автообновление
  // ---------------------------------------------------------------------

  function startPolling() {
    stopPolling();
    if (!state.autoRefresh) return;
    state.pollTimer = setInterval(function () {
      refreshQueue();
    }, POLL_INTERVAL_MS);
  }

  function stopPolling() {
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  function onAutoRefreshToggle(ev) {
    state.autoRefresh = ev.target.checked;
    if (state.autoRefresh) {
      refreshQueue();
      startPolling();
    } else {
      stopPolling();
    }
  }

  // ---------------------------------------------------------------------
  // Инициализация
  // ---------------------------------------------------------------------

  function init() {
    cacheDom();

    dom.loginBtn.addEventListener('click', handleLoginClick);
    dom.tokenInput.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') handleLoginClick();
    });
    dom.logoutBtn.addEventListener('click', handleLogout);
    dom.statusTabs.addEventListener('click', onTabClick);
    dom.autoRefreshToggle.addEventListener('change', onAutoRefreshToggle);

    renderTicketCard();

    var savedToken = null;
    try { savedToken = localStorage.getItem(TOKEN_KEY); } catch (e) {}

    if (savedToken) {
      dom.tokenInput.value = savedToken;
      attemptLogin(savedToken, { silent: true });
    }
  }

  document.addEventListener('DOMContentLoaded', init);
})();
