// Помощник поддержки МИСИС — фронтенд.
//
// window.USE_MOCK = true подменяет все запросы к API заготовленными данными —
// страховка на случай, если на защите отвалится сеть. По умолчанию выключено:
// интерфейс ходит в настоящий API (см. docs/API.md).
window.USE_MOCK = false;
window.MOCK_CASE = 'two_questions';

(function () {
  'use strict';

  const ROLES = {
    applicant: 'Поступающий',
    student: 'Студент',
    teacher: 'Преподаватель'
  };

  const ROLE_GREETINGS = {
    applicant: 'Здравствуйте! Спросите про поступление: сроки, документы, баллы, общежитие.',
    student: 'Привет! Расписание, сессия, стипендия, справки, общежитие — готов ответить на любой вопрос.',
    teacher: 'Здравствуйте! Проконсультирую по доступам к системам, расписанию аудиторий и оформлению документов.'
  };

  const ROLE_SUGGESTIONS = {
    applicant: [
      { label: 'Сроки', query: 'Сроки подачи документов' },
      { label: 'Документы', query: 'Необходимые документы для поступления' },
      { label: 'Баллы', query: 'Минимальные и проходные баллы' },
      { label: 'Общежитие', query: 'Предоставление общежития поступающим' }
    ],
    student: [
      { label: 'Сессия', query: 'Сроки и правила сдачи сессии' },
      { label: 'Стипендия', query: 'Стипендии и сроки выплат' },
      { label: 'Справки', query: 'Как заказать справку об обучении' },
      { label: 'Общежитие', query: 'Проживание в общежитии и оплата' }
    ],
    teacher: [
      { label: 'Доступы к системам', query: 'Доступы к корпоративным системам и сервисам' },
      { label: 'Оформление документов', query: 'Оформление служебных записок и документов' }
    ]
  };

  const STATUS_LABELS = {
    new: 'Принято',
    in_progress: 'В работе',
    answered: 'Есть ответ',
    closed: 'Закрыто'
  };

  const CATEGORY_RU = {
    admission: 'Поступление',
    study: 'Учёба',
    campus_life: 'Студенческая жизнь',
    account: 'Доступы',
    documents: 'Документы',
    other: 'Прочее'
  };

  const LESSON_KIND_LABELS = { lecture: 'лекция', practice: 'практика', lab: 'лаборатор.', other: 'занятие' };
  const WEEK_LABELS = { upper: 'верхняя неделя', lower: 'нижняя неделя' };
  const MONTHS_RU = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
    'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];

  const TICKET_POLL_MS = 20000;

  // ---------------------------------------------------------------------
  // Состояние
  // ---------------------------------------------------------------------

  const state = {
    role: localStorage.getItem('misis_role') || 'student',
    token: localStorage.getItem('misis_token') || null,
    userEmail: localStorage.getItem('misis_email') || null,
    profile: JSON.parse(localStorage.getItem('misis_profile') || 'null'),

    chatId: null,
    ticketModeArmed: false,
    lastFailedMessage: '',

    // Куда вернуться после подтверждения почты: 'ticket' | 'myTickets'.
    postVerifyAction: 'ticket',
    pendingEmail: null,

    resendTimerId: null,
    codeSecondsLeft: 0,

    // Черновик текущего обращения: копится по мере прохождения экранов.
    currentTicketDraft: null, // { requestId, questionIds, answers, draftId }
    finalTicket: null,        // { draftId, subject, body }
    lastSentTicketId: null,

    // Продолжение после заполнения/пропуска профиля (по умолчанию — оформить обращение).
    profileContinuation: null,

    myTickets: {
      list: null,
      view: 'list',       // 'list' | 'detail'
      selectedId: null,
      detail: null,
      pollTimer: null
    }
  };

  // ---------------------------------------------------------------------
  // DOM
  // ---------------------------------------------------------------------

  const screens = {
    main: document.getElementById('screen-main'),
    chat: document.getElementById('screen-chat'),
    verifyEmail: document.getElementById('screen-verify-email'),
    verifyCode: document.getElementById('screen-verify-code'),
    profile: document.getElementById('screen-profile'),
    ticketPreview: document.getElementById('screen-ticket-preview'),
    ticketSent: document.getElementById('screen-ticket-sent'),
    myTickets: document.getElementById('screen-my-tickets')
  };

  const hdrRoleBadge = document.getElementById('hdrRoleBadge');
  const hdrVerifiedBadge = document.getElementById('hdrVerifiedBadge');
  const hdrLogoutBtn = document.getElementById('hdrLogoutBtn');
  const btnLogoHome = document.getElementById('btnLogoHome');
  const btnMyTicketsNav = document.getElementById('btnMyTicketsNav');
  const btnProfileNav = document.getElementById('btnProfileNav');

  const roleSliderButtons = document.querySelectorAll('.role-option');
  const btnStartChat = document.getElementById('btnStartChat');

  const chatFeed = document.getElementById('chatFeed');
  const chatInput = document.getElementById('chatInput');
  const chatSendBtn = document.getElementById('chatSendBtn');
  const tabAskBtn = document.getElementById('tabAskBtn');
  const tabTicketBtn = document.getElementById('tabTicketBtn');
  const scheduleQuickBar = document.getElementById('scheduleQuickBar');

  const emailForm = document.getElementById('emailForm');
  const inputEmail = document.getElementById('inputEmail');
  const emailHintText = document.getElementById('emailHintText');
  const emailStatusBox = document.getElementById('emailStatusBox');
  const btnCancelVerify = document.getElementById('btnCancelVerify');

  const codeForm = document.getElementById('codeForm');
  const inputVerifyCode = document.getElementById('inputVerifyCode');
  const codeStatusBox = document.getElementById('codeStatusBox');
  const btnResendCode = document.getElementById('btnResendCode');
  const codeTimerText = document.getElementById('codeTimerText');
  const verifyCodeTargetEmail = document.getElementById('verifyCodeTargetEmail');
  const demoCodeBanner = document.getElementById('demoCodeBanner');

  const profileDynamicFields = document.getElementById('profileDynamicFields');
  const profileNotReadyBox = document.getElementById('profileNotReadyBox');
  const profileStatusBox = document.getElementById('profileStatusBox');
  const btnSkipProfile = document.getElementById('btnSkipProfile');
  const btnProfileNotReadyContinue = document.getElementById('btnProfileNotReadyContinue');
  const profileForm = document.getElementById('profileForm');

  const draftLoadingBox = document.getElementById('draftLoadingBox');
  const draftErrorBox = document.getElementById('draftErrorBox');
  const clarifyQuestionsBlock = document.getElementById('clarifyQuestionsBlock');
  const clarifyInputsContainer = document.getElementById('clarifyInputsContainer');
  const ticketReadyBlock = document.getElementById('ticketReadyBlock');
  const submitStatusBox = document.getElementById('submitStatusBox');

  const myTicketsListView = document.getElementById('myTicketsListView');
  const myTicketsDetailView = document.getElementById('myTicketsDetailView');
  const myTicketsListBox = document.getElementById('myTicketsListBox');
  const ticketDetailBox = document.getElementById('ticketDetailBox');
  const ticketSearchInput = document.getElementById('ticketSearchInput');
  const ticketSearchStatus = document.getElementById('ticketSearchStatus');

  // ---------------------------------------------------------------------
  // Утилиты
  // ---------------------------------------------------------------------

  function escapeHtml(str) {
    if (str === null || str === undefined) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function formatDateTime(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '—';
    const pad = (n) => (n < 10 ? '0' + n : '' + n);
    return pad(d.getDate()) + '.' + pad(d.getMonth() + 1) + '.' + d.getFullYear() +
      ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  function isoDate(d) {
    const pad = (n) => (n < 10 ? '0' + n : '' + n);
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }

  function humanDate(iso) {
    const parts = (iso || '').split('-');
    if (parts.length !== 3) return iso || '';
    const day = parseInt(parts[2], 10);
    const monthIdx = parseInt(parts[1], 10) - 1;
    return `${day} ${MONTHS_RU[monthIdx] || ''}`;
  }

  // ---------------------------------------------------------------------
  // Сетевой слой: реальный API + описание ошибок + мок
  // ---------------------------------------------------------------------

  function apiRequest(path, opts) {
    opts = opts || {};
    if (window.USE_MOCK) {
      return mockRequest(path, opts);
    }

    const headers = {};
    if (opts.body) headers['Content-Type'] = 'application/json';
    if (opts.token) headers['Authorization'] = 'Bearer ' + opts.token;

    return fetch(path, { method: opts.method || 'GET', headers, body: opts.body }).then(
      (res) => res.text().then((text) => {
        let data = null;
        if (text) { try { data = JSON.parse(text); } catch (e) { data = null; } }
        if (!res.ok) {
          const errBody = (data && data.error) || {};
          const err = new Error(errBody.message || `Ошибка сервера (${res.status})`);
          err.status = res.status;
          err.code = errBody.code || null;
          err.details = errBody.details || {};
          throw err;
        }
        return data || {};
      }),
      () => {
        const err = new Error('Нет соединения с сервером');
        err.kind = 'network';
        throw err;
      }
    );
  }

  // Единое описание ошибки для баннеров/состояний — 429 обрабатываем отдельно
  // от прочих ошибок, как того требует контракт (docs/API.md).
  function describeError(err) {
    if (!err) return 'Не удалось выполнить запрос.';
    if (err.kind === 'network') return 'Нет соединения с сервером.';
    if (err.status === 429) {
      const sec = err.details && err.details.retry_after_sec;
      // retry_after_sec приходит только у настоящего rate_limited (docs/API.md);
      // у too_many_attempts (код подтверждения) его нет — там ждать бесполезно,
      // нужен новый код, поэтому просто показываем сообщение сервера.
      return sec != null ? `Слишком часто, повторите через ${sec} секунд.` : (err.message || 'Слишком много попыток.');
    }
    if (err.status === 503 && err.code === 'not_ready') return 'Раздел ещё не подключён.';
    return err.message || 'Не удалось выполнить запрос.';
  }

  // Единый вид для загрузки/пустого списка/ошибки с повтором — переиспользуется
  // в "Моих обращениях" и в разделах профиля/расписания. Полностью берёт на
  // себя класс и содержимое переданного контейнера.
  function renderStateBox(container, opts) {
    container.style.display = 'block';
    container.className = 'state-box' + (opts.error ? ' state-error' : '');
    container.innerHTML = escapeHtml(opts.text) +
      (opts.onRetry ? '<div><button type="button" class="btn btn-secondary state-retry-btn">Повторить</button></div>' : '');
    if (opts.onRetry) {
      const btn = container.querySelector('.state-retry-btn');
      if (btn) btn.addEventListener('click', opts.onRetry);
    }
  }

  // ---------------------------------------------------------------------
  // Экраны
  // ---------------------------------------------------------------------

  function showScreen(name) {
    Object.keys(screens).forEach((key) => {
      if (screens[key]) screens[key].classList.toggle('is-active', key === name);
    });
    window.scrollTo(0, 0);
    updateHeaderUI();

    if (name === 'verifyEmail') updateEmailScreenForRole();
    if (name !== 'myTickets') stopTicketPolling();
  }

  function updateEmailScreenForRole() {
    if (state.role === 'applicant') {
      inputEmail.placeholder = 'Введите почту';
      emailHintText.style.display = 'none';
    } else {
      inputEmail.placeholder = 'ivanov@edu.misis.ru';
      emailHintText.style.display = 'block';
    }
  }

  function updateHeaderUI() {
    hdrRoleBadge.textContent = ROLES[state.role];
    btnProfileNav.style.display = state.role === 'applicant' ? 'none' : 'inline-block';
    if (state.token && state.userEmail) {
      hdrVerifiedBadge.style.display = 'inline-flex';
      hdrVerifiedBadge.textContent = `✓ ${state.userEmail}`;
      hdrLogoutBtn.style.display = 'inline-block';
    } else {
      hdrVerifiedBadge.style.display = 'none';
      hdrLogoutBtn.style.display = 'none';
    }

    const isQualifiedForSchedule = !!state.token && (state.role === 'student' || state.role === 'teacher') &&
      state.profile && (state.profile.group || state.profile.department);
    scheduleQuickBar.style.display = isQualifiedForSchedule ? 'flex' : 'none';
  }

  function setRole(newRole) {
    state.role = newRole;
    localStorage.setItem('misis_role', newRole);
    roleSliderButtons.forEach((btn) => {
      const isCur = btn.getAttribute('data-role') === newRole;
      btn.classList.toggle('is-selected', isCur);
      btn.setAttribute('aria-checked', isCur ? 'true' : 'false');
    });
    updateHeaderUI();
    updateEmailScreenForRole();
    renderRoleSuggestions();
  }

  function renderRoleSuggestions() {
    const container = document.getElementById('roleSuggestionsBar');
    if (!container) return;
    container.innerHTML = '';
    (ROLE_SUGGESTIONS[state.role] || []).forEach((item) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'pill-btn';
      btn.textContent = item.label;
      btn.setAttribute('title', item.query);
      btn.addEventListener('click', () => {
        chatInput.value = item.query;
        submitChatMessage();
      });
      container.appendChild(btn);
    });
  }

  roleSliderButtons.forEach((btn) => {
    btn.addEventListener('click', () => setRole(btn.getAttribute('data-role')));
  });

  btnStartChat.addEventListener('click', () => {
    showScreen('chat');
    ensureInitialGreeting();
  });

  btnLogoHome.addEventListener('click', () => showScreen('main'));
  btnLogoHome.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); showScreen('main'); }
  });

  btnMyTicketsNav.addEventListener('click', () => {
    showScreen('myTickets');
    showMyTicketsListView();
    if (state.token) {
      loadMyTickets();
    } else {
      renderStateBox(myTicketsListBox, {
        text: 'Список личных обращений виден после подтверждения почты. Можно найти обращение по номеру выше.'
      });
    }
  });

  btnProfileNav.addEventListener('click', () => {
    state.profileContinuation = () => showScreen('chat');
    if (!state.token) {
      state.postVerifyAction = 'profile';
      showScreen('verifyEmail');
    } else {
      loadProfileForm();
    }
  });

  hdrLogoutBtn.addEventListener('click', () => {
    state.token = null;
    state.userEmail = null;
    state.profile = null;
    localStorage.removeItem('misis_token');
    localStorage.removeItem('misis_email');
    localStorage.removeItem('misis_profile');
    updateHeaderUI();
    addBotTextBubble('Вы вышли из подтверждённого аккаунта. Обращения можно создавать после повторного подтверждения почты.');
  });

  // ---------------------------------------------------------------------
  // Чат: разбор вопроса (/api/ask)
  // ---------------------------------------------------------------------

  function ensureInitialGreeting() {
    if (chatFeed.children.length === 0) addBotTextBubble(ROLE_GREETINGS[state.role]);
  }

  function addBotTextBubble(text) {
    const group = document.createElement('div');
    group.className = 'msg-bubble msg-bubble-bot';
    const card = document.createElement('div');
    card.className = 'answer-card';
    card.style.borderLeft = '4px solid var(--brand)';
    card.innerHTML = `<div class="answer-card-body" style="margin-bottom:0;">${escapeHtml(text)}</div>`;
    group.appendChild(card);
    chatFeed.appendChild(group);
    scrollChatToBottom();
  }

  function addUserBubble(text) {
    const tpl = document.getElementById('tpl-user-msg');
    const node = tpl.content.cloneNode(true);
    node.querySelector('.user-text').textContent = text;
    chatFeed.appendChild(node);
    scrollChatToBottom();
  }

  function showTypingIndicator() {
    const div = document.createElement('div');
    div.id = 'chatTypingNode';
    div.className = 'typing-indicator';
    div.innerHTML = `
      <span class="dot-flashing"></span>
      <span class="dot-flashing"></span>
      <span class="dot-flashing"></span>
      <span style="margin-left:4px;">Разбираю обращение…</span>
    `;
    chatFeed.appendChild(div);
    scrollChatToBottom();
  }

  function hideTypingIndicator() {
    const node = document.getElementById('chatTypingNode');
    if (node) node.remove();
  }

  function scrollChatToBottom() {
    chatFeed.scrollTop = chatFeed.scrollHeight;
  }

  function renderBotQuestionsResponse(response) {
    const containerTpl = document.getElementById('tpl-bot-container');
    const container = containerTpl.content.cloneNode(true);
    const groupEl = container.querySelector('.bot-answer-group');

    const ansTpl = document.getElementById('tpl-answer-card');
    const notFoundTpl = document.getElementById('tpl-notfound-card');

    (response.questions || []).forEach((q) => {
      if (q.answer) {
        const cardNode = ansTpl.content.cloneNode(true);
        cardNode.querySelector('.answer-card-header').textContent = q.question;
        cardNode.querySelector('.answer-card-body').textContent = q.answer.summary;

        const sourcesBox = cardNode.querySelector('.answer-sources');
        (q.answer.sources || []).forEach((src) => {
          const a = document.createElement('a');
          a.className = 'answer-source-link';
          a.target = '_blank';
          a.rel = 'noopener';
          a.href = src.url;
          a.innerHTML = `→ <span>${escapeHtml(src.title)}</span> ↗`;
          sourcesBox.appendChild(document.createElement('br'));
          sourcesBox.appendChild(a);
        });
        groupEl.appendChild(cardNode);
      } else {
        const nfNode = notFoundTpl.content.cloneNode(true);
        nfNode.querySelector('.not-found-title').textContent = q.question;
        const btnTicket = nfNode.querySelector('.btn-create-ticket-trigger');
        btnTicket.addEventListener('click', () => {
          startTicketFlow({ requestId: response.request_id, questionIds: [q.id] });
        });
        groupEl.appendChild(nfNode);
      }
    });

    chatFeed.appendChild(container);
    scrollChatToBottom();

    if (state.ticketModeArmed) {
      state.ticketModeArmed = false;
      resetTicketModeUI();
      startTicketFlow({
        requestId: response.request_id,
        questionIds: (response.questions || []).map((q) => q.id)
      });
    }
  }

  function renderErrorMessage(errorText, onRetry) {
    const tpl = document.getElementById('tpl-error-msg');
    const node = tpl.content.cloneNode(true);
    node.querySelector('.err-desc').textContent = errorText;
    node.querySelector('.btn-retry-send').addEventListener('click', () => { if (onRetry) onRetry(); });
    chatFeed.appendChild(node);
    scrollChatToBottom();
  }

  chatInput.addEventListener('input', () => {
    chatInput.style.height = 'auto';
    chatInput.style.height = Math.min(chatInput.scrollHeight, 120) + 'px';
    chatSendBtn.disabled = chatInput.value.trim().length === 0;
  });

  chatInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submitChatMessage(); }
  });

  chatSendBtn.addEventListener('click', submitChatMessage);

  async function submitChatMessage() {
    const text = chatInput.value.trim();
    if (!text) return;

    addUserBubble(text);
    state.lastFailedMessage = text;
    chatInput.value = '';
    chatInput.style.height = 'auto';
    chatSendBtn.disabled = true;

    if (!state.chatId) state.chatId = 'c_' + Math.random().toString(36).slice(2, 10);

    showTypingIndicator();

    try {
      const response = await apiRequest('/api/ask', {
        method: 'POST',
        body: JSON.stringify({ text, role: state.role, chat_id: state.chatId })
      });
      hideTypingIndicator();
      state.lastFailedMessage = '';
      renderBotQuestionsResponse(response);
    } catch (err) {
      hideTypingIndicator();
      renderErrorMessage(describeError(err), () => {
        chatInput.value = state.lastFailedMessage;
        chatInput.focus();
        submitChatMessage();
      });
    }
  }

  tabAskBtn.addEventListener('click', () => {
    tabAskBtn.classList.add('is-active');
    tabTicketBtn.classList.remove('is-active');
    resetTicketModeUI();
    showScreen('chat');
  });

  // "Создать обращение" в реальном контракте требует request_id из уже
  // разобранного вопроса (POST /api/ticket/draft), поэтому вкладка не прыгает
  // сразу на экран обращения, а просит описать вопрос в том же поле ввода —
  // как только ответ придёт, все вопросы этого запроса уйдут в черновик.
  tabTicketBtn.addEventListener('click', () => {
    tabTicketBtn.classList.add('is-active');
    tabAskBtn.classList.remove('is-active');
    showScreen('chat');
    if (chatInput.value.trim()) {
      submitChatMessageAsTicket();
    } else {
      state.ticketModeArmed = true;
      chatInput.placeholder = 'Опишите вопрос для обращения и нажмите отправить…';
      chatInput.focus();
      addBotTextBubble('Опишите проблему одним сообщением ниже — оформлю обращение по нему.');
    }
  });

  function submitChatMessageAsTicket() {
    state.ticketModeArmed = true;
    submitChatMessage();
  }

  function resetTicketModeUI() {
    state.ticketModeArmed = false;
    chatInput.placeholder = 'Ваш вопрос своими словами...';
  }

  function startTicketFlow(opts) {
    state.currentTicketDraft = {
      requestId: opts.requestId,
      questionIds: opts.questionIds || [],
      answers: {},
      draftId: null
    };
    if (!state.token) {
      state.postVerifyAction = 'ticket';
      showScreen('verifyEmail');
    } else {
      requestTicketDraft();
    }
  }

  // ---------------------------------------------------------------------
  // Подтверждение почты (/api/auth/request-code, /api/auth/verify)
  // ---------------------------------------------------------------------

  btnCancelVerify.addEventListener('click', () => {
    showScreen(state.postVerifyAction === 'myTickets' ? 'myTickets' : 'chat');
  });

  function showEmailStatus(text) {
    emailStatusBox.className = 'status-banner error';
    emailStatusBox.textContent = text;
    emailStatusBox.style.display = 'block';
  }
  function hideEmailStatus() { emailStatusBox.style.display = 'none'; }

  emailForm.addEventListener('submit', async () => {
    const email = inputEmail.value.trim();
    if (!email) return;
    hideEmailStatus();

    const btn = document.getElementById('btnSendCode');
    btn.disabled = true;
    btn.textContent = 'Отправка...';

    try {
      const res = await apiRequest('/api/auth/request-code', {
        method: 'POST',
        body: JSON.stringify({ email })
      });
      state.pendingEmail = email;
      verifyCodeTargetEmail.textContent = email;
      showDemoCodeBanner(res.demo_code);
      showScreen('verifyCode');
      initCodeTimer(res.resend_after_sec || 60);
      hideCodeStatus();
    } catch (err) {
      showEmailStatus(describeError(err));
    } finally {
      btn.disabled = false;
      btn.textContent = 'Получить код';
    }
  });

  function showDemoCodeBanner(code) {
    if (!code) {
      demoCodeBanner.style.display = 'none';
      demoCodeBanner.innerHTML = '';
      return;
    }
    demoCodeBanner.innerHTML = `Демо-режим: код <strong>${escapeHtml(code)}</strong>. В рабочем контуре он придёт письмом.`;
    demoCodeBanner.style.display = 'block';
  }

  function initCodeTimer(seconds) {
    state.codeSecondsLeft = seconds;
    clearInterval(state.resendTimerId);
    btnResendCode.disabled = true;
    updateTimerLabel();
    state.resendTimerId = setInterval(() => {
      state.codeSecondsLeft--;
      updateTimerLabel();
      if (state.codeSecondsLeft <= 0) {
        clearInterval(state.resendTimerId);
        btnResendCode.disabled = false;
        codeTimerText.textContent = '';
      }
    }, 1000);
  }

  function updateTimerLabel() {
    if (state.codeSecondsLeft > 0) {
      const m = Math.floor(state.codeSecondsLeft / 60);
      const s = state.codeSecondsLeft % 60;
      codeTimerText.textContent = `(${m}:${s < 10 ? '0' : ''}${s})`;
    }
  }

  btnResendCode.addEventListener('click', async () => {
    try {
      const res = await apiRequest('/api/auth/request-code', {
        method: 'POST',
        body: JSON.stringify({ email: state.pendingEmail })
      });
      showDemoCodeBanner(res.demo_code);
      initCodeTimer(res.resend_after_sec || 60);
      showCodeStatus('Новый код выслан.', 'info');
    } catch (err) {
      showCodeStatus(describeError(err), 'error');
    }
  });

  function showCodeStatus(text, type) {
    codeStatusBox.className = `status-banner ${type || 'info'}`;
    codeStatusBox.textContent = text;
    codeStatusBox.style.display = 'block';
  }
  function hideCodeStatus() { codeStatusBox.style.display = 'none'; }

  codeForm.addEventListener('submit', async () => {
    const code = inputVerifyCode.value.trim();
    if (code.length < 6) {
      showCodeStatus('Введите все 6 цифр кода подтверждения', 'warn');
      return;
    }

    const submitBtn = document.getElementById('btnSubmitCode');
    submitBtn.disabled = true;
    submitBtn.textContent = 'Проверка...';

    try {
      const res = await apiRequest('/api/auth/verify', {
        method: 'POST',
        body: JSON.stringify({ email: state.pendingEmail, code })
      });

      state.token = res.token;
      state.userEmail = state.pendingEmail;
      localStorage.setItem('misis_token', state.token);
      localStorage.setItem('misis_email', state.userEmail);
      updateHeaderUI();

      if (state.postVerifyAction === 'myTickets') {
        showScreen('myTickets');
        showMyTicketsListView();
        loadMyTickets();
        return;
      }

      if (state.postVerifyAction === 'profile') {
        loadProfileForm();
        return;
      }

      if (state.role !== 'applicant' && !state.profile) {
        state.profileContinuation = requestTicketDraft;
        loadProfileForm();
      } else {
        requestTicketDraft();
      }
    } catch (err) {
      if (err.status === 400 && err.code === 'invalid_code') {
        const left = err.details && err.details.attempts_left;
        showCodeStatus(`Неверный код${left != null ? `, осталось попыток: ${left}` : ''}`, 'error');
      } else if (err.status === 410 && err.code === 'code_expired') {
        showCodeStatus('Код истёк, запросите новый', 'warn');
        btnResendCode.disabled = false;
      } else if (err.status === 429) {
        showCodeStatus(describeError(err), 'warn');
      } else {
        showCodeStatus(describeError(err), 'error');
      }
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = 'Подтвердить код';
    }
  });

  // ---------------------------------------------------------------------
  // Профиль (/api/profile/fields, /api/profile) — модуль может быть не готов
  // ---------------------------------------------------------------------

  function loadProfileForm() {
    showScreen('profile');
    profileNotReadyBox.style.display = 'none';
    profileStatusBox.style.display = 'none';
    profileForm.style.display = 'block';
    profileDynamicFields.innerHTML = '<div class="state-box">Загрузка полей профиля…</div>';

    apiRequest(`/api/profile/fields?role=${encodeURIComponent(state.role)}`).then((data) => {
      if (!data.fields || data.fields.length === 0) {
        continueAfterProfile();
        return;
      }
      renderProfileFields(data.fields);
    }, (err) => {
      profileForm.style.display = 'none';
      profileNotReadyBox.style.display = 'block';
      // Текст по умолчанию в разметке — про not_ready; для прочих ошибок подменяем
      // только первый (текстовый) узел, не трогая кнопку "Продолжить без профиля".
      if (!(err.status === 503 && err.code === 'not_ready')) {
        profileNotReadyBox.firstChild.textContent = describeError(err) + ' ';
      } else {
        profileNotReadyBox.firstChild.textContent =
          'Раздел ещё не подключён. Можно продолжить без профиля — расписание и автозаполнение будут недоступны.';
      }
    });
  }

  function renderProfileFields(fields) {
    profileDynamicFields.innerHTML = '';
    fields.forEach((f) => {
      const grp = document.createElement('div');
      grp.className = 'form-group';
      const lbl = document.createElement('label');
      lbl.className = 'form-label';
      lbl.htmlFor = 'fld_' + f.name;
      lbl.innerHTML = `${escapeHtml(f.label)} ${f.required ? '<span class="req">*</span>' : ''}`;
      grp.appendChild(lbl);

      let inputEl;
      if (f.type === 'select') {
        inputEl = document.createElement('select');
        inputEl.className = 'input-field';
        inputEl.id = 'fld_' + f.name;
        inputEl.name = f.name;
        if (f.required) inputEl.required = true;

        const defOpt = document.createElement('option');
        defOpt.value = '';
        defOpt.textContent = 'Выберите из списка...';
        inputEl.appendChild(defOpt);

        (f.options || []).forEach((opt) => {
          const o = document.createElement('option');
          o.value = opt;
          o.textContent = opt;
          inputEl.appendChild(o);
        });
      } else {
        inputEl = document.createElement('input');
        inputEl.type = 'text';
        inputEl.className = 'input-field';
        inputEl.id = 'fld_' + f.name;
        inputEl.name = f.name;
        if (f.required) inputEl.required = true;
      }
      grp.appendChild(inputEl);
      profileDynamicFields.appendChild(grp);
    });
  }

  function continueAfterProfile() {
    const fn = state.profileContinuation || requestTicketDraft;
    state.profileContinuation = null;
    fn();
  }

  profileForm.addEventListener('submit', async () => {
    const formData = new FormData(profileForm);
    const values = {};
    formData.forEach((v, k) => { values[k] = v; });

    profileStatusBox.style.display = 'none';
    try {
      const res = await apiRequest('/api/profile', {
        method: 'POST',
        token: state.token,
        body: JSON.stringify({ values })
      });
      state.profile = res.values || values;
      localStorage.setItem('misis_profile', JSON.stringify(state.profile));
      updateHeaderUI();
      continueAfterProfile();
    } catch (err) {
      profileStatusBox.style.display = 'block';
      profileStatusBox.textContent = 'Не удалось сохранить профиль: ' + describeError(err);
    }
  });

  btnSkipProfile.addEventListener('click', () => {
    state.profile = null;
    updateHeaderUI();
    continueAfterProfile();
  });

  btnProfileNotReadyContinue.addEventListener('click', () => {
    continueAfterProfile();
  });

  // ---------------------------------------------------------------------
  // Черновик и отправка обращения (/api/ticket/draft, /api/ticket/submit)
  // ---------------------------------------------------------------------

  function requestTicketDraft(clarifyAnswers) {
    showScreen('ticketPreview');
    draftErrorBox.style.display = 'none';
    clarifyQuestionsBlock.style.display = 'none';
    ticketReadyBlock.style.display = 'none';
    draftLoadingBox.style.display = 'block';

    const draft = state.currentTicketDraft;
    const answers = Object.assign({}, draft.answers, clarifyAnswers || {});

    apiRequest('/api/ticket/draft', {
      method: 'POST',
      body: JSON.stringify({
        request_id: draft.requestId,
        question_ids: draft.questionIds,
        answers,
        draft_id: draft.draftId || undefined
      })
    }).then((res) => {
      draftLoadingBox.style.display = 'none';
      draft.answers = answers;
      draft.draftId = res.draft_id;

      if (!res.ready && res.missing_fields && res.missing_fields.length > 0) {
        clarifyQuestionsBlock.style.display = 'block';
        clarifyInputsContainer.innerHTML = '';
        res.missing_fields.forEach((mf) => {
          const grp = document.createElement('div');
          grp.className = 'form-group';
          grp.innerHTML = `
            <label class="form-label">${escapeHtml(mf.question)} <span class="req">*</span></label>
            <input type="text" class="input-field clarify-input" data-field="${escapeHtml(mf.field)}" required>
          `;
          clarifyInputsContainer.appendChild(grp);
        });
        document.getElementById('btnSubmitClarifications').onclick = () => {
          const filled = {};
          clarifyInputsContainer.querySelectorAll('.clarify-input').forEach((inp) => {
            filled[inp.getAttribute('data-field')] = inp.value;
          });
          requestTicketDraft(filled);
        };
      } else {
        ticketReadyBlock.style.display = 'block';
        submitStatusBox.style.display = 'none';
        document.getElementById('previewSubject').textContent = res.subject;
        document.getElementById('previewSender').textContent = state.userEmail || '—';
        document.getElementById('previewText').textContent = res.body;
        state.finalTicket = { draftId: res.draft_id, subject: res.subject, body: res.body };
      }
    }, (err) => {
      draftLoadingBox.style.display = 'none';
      draftErrorBox.style.display = 'block';
      if (err.code === 'request_not_found') {
        draftErrorBox.innerHTML = 'Информация об обращении устарела, опишите вопрос ещё раз.' +
          '<div><button type="button" class="btn btn-secondary state-retry-btn">Вернуться к вопросам</button></div>';
        draftErrorBox.querySelector('.state-retry-btn').addEventListener('click', () => showScreen('chat'));
      } else {
        renderStateBox(draftErrorBox, {
          text: describeError(err), error: true,
          onRetry: () => requestTicketDraft(clarifyAnswers)
        });
      }
    });
  }

  document.getElementById('btnEditTicket').addEventListener('click', () => showScreen('chat'));

  document.getElementById('btnConfirmSendTicket').addEventListener('click', async () => {
    const btn = document.getElementById('btnConfirmSendTicket');
    btn.disabled = true;
    btn.textContent = 'Отправка...';
    submitStatusBox.style.display = 'none';

    try {
      const res = await apiRequest('/api/ticket/submit', {
        method: 'POST',
        body: JSON.stringify({
          draft_id: state.finalTicket.draftId,
          token: state.token,
          anonymous: false
        })
      });

      state.lastSentTicketId = res.ticket_id;
      const recipientMatch = /^\[MISIS-SUPPORT\]\s*([^/]+)\//.exec(res.subject || '');
      const recipient = recipientMatch ? recipientMatch[1].trim() : 'Служба поддержки МИСИС';

      document.getElementById('sentTicketId').textContent = res.ticket_id;
      document.getElementById('sentTargetDept').textContent = recipient;

      state.currentTicketDraft = null;
      state.finalTicket = null;
      showScreen('ticketSent');
    } catch (err) {
      submitStatusBox.style.display = 'block';
      submitStatusBox.textContent = describeError(err);
      if (err.status === 401) {
        submitStatusBox.textContent = 'Сессия истекла. Подтвердите почту заново.';
      }
    } finally {
      btn.disabled = false;
      btn.textContent = 'Отправить обращение';
    }
  });

  document.getElementById('btnBackToChatAfterSent').addEventListener('click', () => {
    tabAskBtn.classList.add('is-active');
    tabTicketBtn.classList.remove('is-active');
    showScreen('chat');
  });

  document.getElementById('btnOpenTicketAfterSent').addEventListener('click', () => {
    showScreen('myTickets');
    if (state.lastSentTicketId) {
      openTicketDetail(state.lastSentTicketId);
    } else {
      showMyTicketsListView();
      if (state.token) loadMyTickets();
    }
  });

  // ---------------------------------------------------------------------
  // Расписание (/api/schedule) — модуль может быть не готов
  // ---------------------------------------------------------------------

  document.getElementById('btnSchedToday').addEventListener('click', () => fetchSchedule(isoDate(new Date())));
  document.getElementById('btnSchedTomorrow').addEventListener('click', () => {
    const d = new Date();
    d.setDate(d.getDate() + 1);
    fetchSchedule(isoDate(d));
  });
  document.getElementById('dateSchedPicker').addEventListener('change', (e) => {
    if (e.target.value) fetchSchedule(e.target.value);
  });
  document.getElementById('btnSchedPickDay').addEventListener('click', () => {
    const picker = document.getElementById('dateSchedPicker');
    if (typeof picker.showPicker === 'function') {
      try { picker.showPicker(); return; } catch (err) { /* fall through */ }
    }
    picker.focus();
    picker.click();
  });

  function fetchSchedule(dateStr) {
    showTypingIndicator();
    apiRequest(`/api/schedule?date=${encodeURIComponent(dateStr)}`, { token: state.token }).then((data) => {
      hideTypingIndicator();
      renderScheduleCard(data);
    }, (err) => {
      hideTypingIndicator();
      if (err.status === 503 && err.code === 'not_ready') {
        addScheduleNoticeCard('Раздел расписания ещё не подключён.');
      } else if (err.status === 409 && err.code === 'profile_incomplete') {
        const wrap = document.createElement('div');
        wrap.className = 'msg-bubble msg-bubble-bot';
        wrap.innerHTML = `
          <div class="card" style="border-left:4px solid var(--warn);">
            <h4 style="color:var(--warn); margin-bottom:6px;">Чтобы показать расписание, нужна ваша группа</h4>
            <p style="font-size:15px; color:var(--text); margin-bottom:12px;">Заполните профиль студента или преподавателя для доступа к расписанию.</p>
            <button type="button" class="btn btn-secondary" id="btnOpenProfFromSchedule">Заполнить профиль</button>
          </div>
        `;
        chatFeed.appendChild(wrap);
        wrap.querySelector('#btnOpenProfFromSchedule').onclick = () => {
          state.profileContinuation = () => { showScreen('chat'); fetchSchedule(dateStr); };
          loadProfileForm();
        };
        scrollChatToBottom();
      } else if (err.status === 404 && err.code === 'schedule_not_found') {
        addScheduleNoticeCard('Расписание для вашего института пока не загружено.');
      } else {
        renderErrorMessage('Не удалось загрузить расписание: ' + describeError(err), () => fetchSchedule(dateStr));
      }
    });
  }

  function addScheduleNoticeCard(text) {
    const wrap = document.createElement('div');
    wrap.className = 'msg-bubble msg-bubble-bot';
    wrap.innerHTML = `<div class="not-ready-box" style="width:100%;">${escapeHtml(text)}</div>`;
    chatFeed.appendChild(wrap);
    scrollChatToBottom();
  }

  function renderScheduleCard(sched) {
    const wrap = document.createElement('div');
    wrap.className = 'msg-bubble msg-bubble-bot';
    const dateLabel = `${sched.day || ''}, ${humanDate(sched.date)}`;

    if (!sched.lessons || sched.lessons.length === 0) {
      wrap.innerHTML = `
        <div class="card" style="border-left:4px solid var(--brand); width:100%;">
          <strong>${escapeHtml(dateLabel)}</strong><br>
          <span style="color:var(--text-dim);">В этот день занятий нет.</span>
        </div>
      `;
      chatFeed.appendChild(wrap);
      scrollChatToBottom();
      return;
    }

    const tpl = document.getElementById('tpl-schedule-card');
    const card = tpl.content.cloneNode(true);
    card.querySelector('.sched-date-title').textContent = dateLabel;
    card.querySelector('.sched-week-title').textContent = WEEK_LABELS[sched.week] || '';

    const listContainer = card.querySelector('.schedule-lessons-list');
    const itemTpl = document.getElementById('tpl-schedule-item');

    sched.lessons.forEach((l) => {
      const itemNode = itemTpl.content.cloneNode(true);
      itemNode.querySelector('.time-hours').textContent = `${l.time_start}–${l.time_end}`;
      itemNode.querySelector('.time-num').textContent = `${l.pair} пара`;
      itemNode.querySelector('.lesson-title').textContent = l.subject;

      const kindBadge = itemNode.querySelector('.badge-lesson-kind');
      kindBadge.textContent = LESSON_KIND_LABELS[l.kind] || 'занятие';
      kindBadge.className = `badge-lesson-kind kind-${l.kind || 'other'}`;

      const subEl = itemNode.querySelector('.lesson-sub');
      const room = l.room + (l.subgroup ? `, подгруппа ${l.subgroup}` : '');
      if (l.teacher) {
        subEl.innerHTML = `<span>${escapeHtml(l.teacher)}</span> · <span>${escapeHtml(room)}</span>`;
      } else {
        subEl.innerHTML = `<span>${escapeHtml(room)}</span>`;
      }

      listContainer.appendChild(itemNode);
    });

    wrap.appendChild(card);
    chatFeed.appendChild(wrap);
    scrollChatToBottom();
  }

  // ---------------------------------------------------------------------
  // "Мои обращения" — почта отменена, это единственное место с ответом
  // ---------------------------------------------------------------------

  function showMyTicketsListView() {
    state.myTickets.view = 'list';
    myTicketsListView.style.display = 'block';
    myTicketsDetailView.style.display = 'none';
    stopTicketPolling();
  }

  function showMyTicketsDetailView() {
    state.myTickets.view = 'detail';
    myTicketsListView.style.display = 'none';
    myTicketsDetailView.style.display = 'block';
  }

  document.getElementById('btnRefreshMyTickets').addEventListener('click', () => {
    if (state.token) loadMyTickets();
  });

  document.getElementById('btnBackToTicketsList').addEventListener('click', () => {
    showMyTicketsListView();
  });

  document.getElementById('btnRefreshTicketDetail').addEventListener('click', () => {
    if (state.myTickets.selectedId) loadTicketDetail(state.myTickets.selectedId);
  });

  document.getElementById('btnTicketSearch').addEventListener('click', runTicketSearch);
  ticketSearchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); runTicketSearch(); }
  });

  function runTicketSearch() {
    const id = ticketSearchInput.value.trim();
    ticketSearchStatus.style.display = 'none';
    if (!id) return;

    apiRequest(`/api/ticket/${encodeURIComponent(id)}`, { token: state.token || undefined }).then((card) => {
      state.myTickets.selectedId = card.ticket_id;
      state.myTickets.detail = card;
      showMyTicketsDetailView();
      renderTicketDetail(card);
      startTicketPolling();
    }, (err) => {
      ticketSearchStatus.style.display = 'block';
      if (err.status === 404) {
        ticketSearchStatus.textContent = 'Обращение с таким номером не найдено.';
      } else if (err.status === 403) {
        ticketSearchStatus.textContent = 'Это обращение привязано к другой почте — по номеру недоступно.';
      } else {
        ticketSearchStatus.textContent = describeError(err);
      }
    });
  }

  function loadMyTickets() {
    renderStateBox(myTicketsListBox, { text: 'Загрузка обращений…' });
    apiRequest('/api/my/tickets', { token: state.token }).then((data) => {
      state.myTickets.list = data.tickets || [];
      renderMyTicketsList();
    }, (err) => {
      if (err.status === 401) {
        state.token = null;
        localStorage.removeItem('misis_token');
        updateHeaderUI();
        renderStateBox(myTicketsListBox, {
          text: 'Сессия истекла, подтвердите почту заново.',
          error: true,
          onRetry: () => { state.postVerifyAction = 'myTickets'; showScreen('verifyEmail'); }
        });
        return;
      }
      renderStateBox(myTicketsListBox, { text: describeError(err), error: true, onRetry: loadMyTickets });
    });
  }

  function renderMyTicketsList() {
    const list = state.myTickets.list || [];
    if (list.length === 0) {
      renderStateBox(myTicketsListBox, { text: 'Обращений пока нет.' });
      return;
    }
    myTicketsListBox.innerHTML = `<div class="tickets-list">${list.map(renderTicketRowHtml).join('')}</div>`;
    myTicketsListBox.querySelectorAll('.ticket-list-item').forEach((el) => {
      el.addEventListener('click', () => openTicketDetail(el.getAttribute('data-id')));
    });
  }

  function renderTicketRowHtml(t) {
    const statusClass = 'status-chip-' + (t.status || 'new');
    const statusLabel = STATUS_LABELS[t.status] || t.status;
    return `
      <button type="button" class="ticket-list-item" data-id="${escapeHtml(t.ticket_id)}">
        <div class="ticket-list-row-top">
          <span class="ticket-list-num">${escapeHtml(t.ticket_id)}</span>
          <span class="status-chip ${statusClass}">${escapeHtml(statusLabel)}</span>
        </div>
        <div class="ticket-list-subject">${escapeHtml(t.subject)}</div>
        <div class="ticket-list-row-bottom">
          <span>${escapeHtml(t.recipient || '—')}</span>
          <span>${escapeHtml(formatDateTime(t.last_message_at || t.created_at))}</span>
        </div>
      </button>
    `;
  }

  function openTicketDetail(id) {
    state.myTickets.selectedId = id;
    showMyTicketsDetailView();
    loadTicketDetail(id);
  }

  function loadTicketDetail(id, opts) {
    opts = opts || {};
    if (!opts.silent) renderStateBox(ticketDetailBox, { text: 'Загрузка обращения…' });

    apiRequest(`/api/ticket/${encodeURIComponent(id)}`, { token: state.token || undefined }).then((card) => {
      if (state.myTickets.selectedId !== id) return;
      state.myTickets.detail = card;
      renderTicketDetail(card);
      startTicketPolling();
    }, (err) => {
      if (state.myTickets.selectedId !== id) return;
      renderStateBox(ticketDetailBox, {
        text: err.status === 404 ? 'Обращение не найдено.' :
          err.status === 403 ? 'Обращение привязано к другой почте.' : describeError(err),
        error: true,
        onRetry: () => loadTicketDetail(id)
      });
    });
  }

  function renderTicketDetail(card) {
    const statusClass = 'status-chip-' + (card.status || 'new');
    const statusLabel = STATUS_LABELS[card.status] || card.status;
    const categoryLabel = CATEGORY_RU[card.category] || card.category || '—';

    const messagesHtml = (card.messages || []).map((m) => {
      const cls = m.author === 'operator' ? 'thread-msg-operator' : 'thread-msg-user';
      const who = m.author === 'operator' ? 'Сотрудник' : 'Вы';
      return `
        <div class="thread-msg ${cls}">
          <div class="thread-msg-meta">${escapeHtml(who)} · ${escapeHtml(formatDateTime(m.created_at))}</div>
          <div>${escapeHtml(m.text)}</div>
        </div>
      `;
    }).join('');

    ticketDetailBox.innerHTML = `
      <h2 style="font-size:20px; font-weight:700; color:var(--brand); margin-bottom:10px;">
        ${escapeHtml(card.ticket_id)} — ${escapeHtml(card.subject)}
      </h2>
      <div class="ticket-detail-meta">
        <span class="status-chip ${statusClass}">${escapeHtml(statusLabel)}</span>
        <span class="meta-chip">${escapeHtml(categoryLabel)}</span>
        <span class="meta-chip">${escapeHtml(card.recipient || '—')}</span>
        ${card.anonymous ? '<span class="meta-chip">анонимное</span>' : ''}
      </div>
      <div class="thread-feed">
        <div class="thread-original">${escapeHtml(card.body || '')}</div>
        ${messagesHtml}
      </div>
      <div class="clarify-row">
        <textarea id="ticketMessageInput" class="chat-textarea" rows="1" placeholder="Уточнить…" aria-label="Уточнить обращение"></textarea>
        <button type="button" class="btn btn-primary" id="btnSendTicketMessage">Отправить</button>
      </div>
      <div id="ticketMessageStatus" class="status-banner error" style="display:none; margin-top:10px;"></div>
    `;

    document.getElementById('btnSendTicketMessage').addEventListener('click', () => sendTicketMessage(card.ticket_id));
    document.getElementById('ticketMessageInput').addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendTicketMessage(card.ticket_id); }
    });

    const feed = ticketDetailBox.querySelector('.thread-feed');
    feed.scrollTop = feed.scrollHeight;
  }

  function sendTicketMessage(ticketId) {
    const input = document.getElementById('ticketMessageInput');
    const text = input.value.trim();
    if (!text) return;

    const btn = document.getElementById('btnSendTicketMessage');
    const statusBox = document.getElementById('ticketMessageStatus');
    btn.disabled = true;
    btn.textContent = 'Отправляю…';
    statusBox.style.display = 'none';

    apiRequest(`/api/ticket/${encodeURIComponent(ticketId)}/message`, {
      method: 'POST',
      token: state.token || undefined,
      body: JSON.stringify({ text })
    }).then((card) => {
      state.myTickets.detail = card;
      renderTicketDetail(card);
    }, (err) => {
      btn.disabled = false;
      btn.textContent = 'Отправить';
      statusBox.style.display = 'block';
      statusBox.textContent = describeError(err);
    });
  }

  function startTicketPolling() {
    stopTicketPolling();
    state.myTickets.pollTimer = setInterval(() => {
      const id = state.myTickets.selectedId;
      if (!id || state.myTickets.view !== 'detail') return;
      const input = document.getElementById('ticketMessageInput');
      if (input && input.value.trim()) return; // не мешаем набирать уточнение
      apiRequest(`/api/ticket/${encodeURIComponent(id)}`, { token: state.token || undefined }).then((card) => {
        if (state.myTickets.selectedId !== id) return;
        const prev = state.myTickets.detail;
        if (!prev || prev.messages.length !== card.messages.length || prev.status !== card.status) {
          state.myTickets.detail = card;
          renderTicketDetail(card);
        }
      }, () => { /* тихий фон — не мешаем пользователю баннером об ошибке */ });
    }, TICKET_POLL_MS);
  }

  function stopTicketPolling() {
    if (state.myTickets.pollTimer) {
      clearInterval(state.myTickets.pollTimer);
      state.myTickets.pollTimer = null;
    }
  }

  // ---------------------------------------------------------------------
  // Мок — страховка на случай сети. Тот же контракт, что и настоящий API.
  // ---------------------------------------------------------------------

  let mockDb = null;

  function buildMockDb() {
    const now = () => new Date().toISOString();
    return {
      draftSeq: 0,
      ticketSeq: 6,
      drafts: {},
      tickets: [
        {
          ticket_id: 'MISIS-2026-0005', subject: '[MISIS-SUPPORT] Учебный отдел / Документы / P3 — Нужна справка об обучении',
          status: 'answered', priority: 'P3', category: 'documents', recipient: 'Учебный отдел',
          anonymous: false, email: 'demo@edu.misis.ru',
          created_at: now(), last_message_at: now(),
          body: 'Здравствуйте! Нужна справка об обучении для военкомата.',
          messages: [{ author: 'operator', text: 'Справка готова, заберите в деканате.', created_at: now() }]
        },
        {
          ticket_id: 'MISIS-2026-0006', subject: '[MISIS-SUPPORT] Студгородок / Студенческая жизнь / P4 — Вопрос по общежитию',
          status: 'new', priority: 'P4', category: 'campus_life', recipient: 'Студгородок',
          anonymous: false, email: 'demo@edu.misis.ru',
          created_at: now(), last_message_at: now(),
          body: 'Когда открывается заселение в общежитие №3?',
          messages: []
        }
      ]
    };
  }

  function mockRequest(path, opts) {
    if (!mockDb) mockDb = buildMockDb();
    return new Promise((resolve, reject) => {
      setTimeout(() => {
        try { resolve(mockHandle(path, opts)); } catch (err) { reject(err); }
      }, 450);
    });
  }

  function mockErr(status, code, message, details) {
    const err = new Error(message);
    err.status = status; err.code = code; err.details = details || {};
    return err;
  }

  function mockHandle(path, opts) {
    const scenario = window.MOCK_CASE;
    const body = opts.body ? JSON.parse(opts.body) : {};
    const [pathname, query] = path.split('?');
    const params = new URLSearchParams(query || '');

    if (pathname === '/api/ask') {
      if (scenario === 'server_error') throw mockErr(500, 'internal', 'Внутренняя ошибка сервиса классификации');
      if (scenario === 'rate_limited') throw mockErr(429, 'rate_limited', 'Слишком часто', { retry_after_sec: 30 });

      const qid = 'q1';
      if (scenario === 'nothing_found') {
        return { request_id: 'r_mock1', questions: [{ id: qid, question: body.text, category: 'other', audience: body.role, answer: null, confidence: 0.1, suggest_ticket: true }], overall_action: 'suggest_ticket', trace: {} };
      }
      return {
        request_id: 'r_mock1',
        questions: [{
          id: qid, question: body.text, category: 'admission', audience: body.role,
          answer: { summary: 'Демо-ответ: приём документов начинается 20 июня и завершается 25 июля.', sources: [{ title: 'Приёмная кампания МИСИС 2026', url: 'https://misis.ru/applicants/' }] },
          confidence: 0.8, suggest_ticket: false
        }],
        overall_action: 'answered', trace: {}
      };
    }

    if (pathname === '/api/ticket/draft') {
      mockDb.draftSeq++;
      const draftId = body.draft_id || 'd_mock' + mockDb.draftSeq;
      const hasProgramme = body.answers && body.answers.programme;
      if (!hasProgramme) {
        mockDb.drafts[draftId] = { ready: false };
        return { draft_id: draftId, subject: '', body: '', missing_fields: [{ field: 'programme', question: 'На какое направление подаёте документы?' }], ready: false };
      }
      mockDb.drafts[draftId] = { ready: true };
      return {
        draft_id: draftId,
        subject: `[MISIS-SUPPORT] Приёмная комиссия / Поступление / P3 / ${draftId} — Демо-обращение`,
        body: `Категория: Поступление\nСуть: демо-обращение\nНаправление: ${body.answers.programme}`,
        missing_fields: [], ready: true
      };
    }

    if (pathname === '/api/auth/request-code') {
      if (scenario === 'rate_limited') throw mockErr(429, 'rate_limited', 'Слишком часто', { retry_after_sec: 30 });
      const res = { ok: true, user_type: 'student', ttl_sec: 600, resend_after_sec: 45 };
      if (scenario === 'demo_code') res.demo_code = '481920';
      return res;
    }

    if (pathname === '/api/auth/verify') {
      if (scenario === 'wrong_code') throw mockErr(400, 'invalid_code', 'Неверный код', { attempts_left: 2 });
      if (scenario === 'expired_code') throw mockErr(410, 'code_expired', 'Код истёк');
      return { token: 'mock-token', user_type: 'student', expires_at: new Date(Date.now() + 1800000).toISOString() };
    }

    if (pathname.startsWith('/api/profile/fields')) {
      const role = params.get('role');
      if (scenario === 'profile_not_ready') throw mockErr(503, 'not_ready', 'Профили ещё не подключены');
      if (role === 'applicant') return { fields: [] };
      if (role === 'teacher') {
        return { fields: [{ name: 'department', label: 'Кафедра', type: 'text', required: true }, { name: 'name', label: 'Фамилия и инициалы', type: 'text', required: true }] };
      }
      return { fields: [
        { name: 'institute', label: 'Институт', type: 'select', required: true, options: ['ИТКН', 'ИНМиН'] },
        { name: 'group', label: 'Учебная группа', type: 'select', required: true, options: ['ББИ-26-6-1'] },
        { name: 'subgroup', label: 'Подгруппа', type: 'select', required: false, options: ['1', '2'] }
      ] };
    }

    if (pathname === '/api/profile' && (!opts.method || opts.method === 'GET')) {
      if (scenario === 'profile_not_ready') throw mockErr(503, 'not_ready', 'Профили ещё не подключены');
      return { role: 'student', filled: false, values: {} };
    }

    if (pathname === '/api/profile' && opts.method === 'POST') {
      if (scenario === 'profile_not_ready') throw mockErr(503, 'not_ready', 'Профили ещё не подключены');
      return { ok: true, values: body.values };
    }

    if (pathname === '/api/schedule') {
      if (scenario === 'schedule_not_ready') throw mockErr(503, 'not_ready', 'Расписание ещё не подключено');
      if (scenario === 'profile_incomplete') throw mockErr(409, 'profile_incomplete', 'Нужна ваша учебная группа');
      const date = params.get('date');
      if (scenario === 'empty_schedule') return { date, day: 'Воскресенье', week: 'upper', group: 'ББИ-26-6-1', lessons: [] };
      return {
        date, day: 'Понедельник', week: 'upper', group: 'ББИ-26-6-1',
        lessons: [
          { pair: 2, time_start: '10:50', time_end: '12:25', subject: 'Введение в специальность', kind: 'lab', teacher: 'Неворошкин В. А.', room: 'Л-812-УВЦ', subgroup: null },
          { pair: 3, time_start: '12:40', time_end: '14:15', subject: 'Математика', kind: 'practice', teacher: 'Ким-Тян Л. Р.', room: 'Л-629', subgroup: null }
        ]
      };
    }

    if (pathname === '/api/ticket/submit') {
      mockDb.ticketSeq++;
      const ticketId = `MISIS-2026-${String(mockDb.ticketSeq).padStart(4, '0')}`;
      return { ticket_id: ticketId, subject: `[MISIS-SUPPORT] Приёмная комиссия / Поступление / P3 / ${ticketId} — Демо-обращение`, delivery: 'inbox', anonymous: !!body.anonymous };
    }

    if (pathname === '/api/my/tickets') {
      return { tickets: mockDb.tickets };
    }

    let m = pathname.match(/^\/api\/ticket\/([^/]+)\/message$/);
    if (m && opts.method === 'POST') {
      const t = mockDb.tickets.find((x) => x.ticket_id === m[1]);
      if (!t) throw mockErr(404, 'ticket_not_found', 'Обращение не найдено');
      t.messages.push({ author: 'user', text: body.text, created_at: new Date().toISOString() });
      t.status = 'in_progress';
      return t;
    }

    m = pathname.match(/^\/api\/ticket\/([^/]+)$/);
    if (m) {
      const t = mockDb.tickets.find((x) => x.ticket_id === m[1]);
      if (!t) throw mockErr(404, 'ticket_not_found', 'Обращение не найдено');
      return t;
    }

    return { status: 'ok' };
  }

  // ---------------------------------------------------------------------
  // Инициализация
  // ---------------------------------------------------------------------

  window.addEventListener('DOMContentLoaded', () => {
    setRole(state.role);
    updateHeaderUI();
    updateEmailScreenForRole();
    renderRoleSuggestions();
  });
})();
