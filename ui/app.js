/* Review workspace. No framework: three screens driven by the URL hash.
   #/                              overview
   #/c/<category>?type=&status=&q=&sort=   list of questions
   #/c/<category>/<id>             one question
   Two languages (nl, en). Legal text is always the original Dutch; everything else follows
   the language switch. Switching re-renders in place and keeps the reviewer's state: the
   open pair, the decision scope, anything typed, filters, scroll position.
   All data-derived text is inserted with textContent, never innerHTML. */
(function () {
  'use strict';

  var STR = {
    en: {
      brand1: 'Legislation', brand2: 'Review', title: 'Legislation Review', skip: 'Skip to content', language: 'Language',
      overview: 'Overview', soon: 'Soon', of: 'of', questionsReviewed: 'questions reviewed', reviewingAs: 'Reviewing as',
      yourName: 'Your name…', shortcuts: 'Keyboard shortcuts', openNav: 'Open navigation', categories: 'Categories',
      eyebrow: 'Dutch and EU digital law', heroTitle: 'The system found the leads. You decide which ones matter.',
      heroText: 'This workspace has read ten laws on data, cybersecurity and AI, and marked places where two provisions may clash or repeat each other. Each one is a lead for a lawyer to judge, not a legal conclusion.',
      start: 'Start reviewing', cont: 'Continue reviewing', how: 'How it works', toReview: 'To review', accepted: 'Accepted', declined: 'Declined',
      statLaws: 'laws read', statQuestions: 'questions to judge', statPairs: 'provision pairs behind them', statDone: 'reviewed so far',
      stepOf: 'Step {a} of 4',
      s1: 'Choose a category', s1t: 'Each category holds one kind of finding, split into smaller groups. The bars on the category cards show how many questions each group has. Click a bar to jump straight to it.',
      s2: 'Open a question', s2t: 'Every question has the same four parts: both provisions in full with the key passage highlighted, a side-by-side comparison, an example situation, and the reasoning for and against.',
      s3: 'Accept or decline', s3t: 'Accept if it is a real issue worth raising. Decline if it is not, or if the system misread the law. The decision panel stays in view while you read, and A and D work as shortcuts.',
      s4: 'Say why when you decline', s4t: 'A reason and a short justification are required for every decline. Those written reasons are what the system learns from. You can edit or remove any decision later.',
      against: 'against', reasonRequired: 'Reason required', notAvailable: 'Not yet available', open: 'Open {a}', goodToKnow: 'Good to know',
      f1q: 'How are the questions ordered?', f1a: 'By how well supported they are. Four bars means the difference was calculated directly from the text with no AI judgement. Three bars means two independent AI checks agreed. Two bars means one AI check looked at it.',
      f2q: 'Why does one question cover several provision pairs?', f2a: 'Two long articles can raise the same question paragraph by paragraph. Instead of showing it thirteen times, the workspace groups it into one question. You decide once, and can decide a single pair separately when it differs.',
      f3q: 'What language is everything in?', f3a: 'Legal text is always shown in the original Dutch. The interface and all explanations follow the language switch at the top right.',
      f4q: 'Can I change a decision?', f4a: 'Yes, at any time. Open the question and choose Edit decision or Remove decision. Earlier versions are kept in a log, so nothing is lost.',
      nAccepted: 'accepted', nDeclined: 'declined', nToReview: 'to review', search: 'Search a law or a topic…', searchLabel: 'Search questions', sortLabel: 'Sort questions',
      sortSupport: 'Best supported first', sortPairs: 'Most provision pairs first', sortLaw: 'By law name', all: 'All', allTypes: 'All types',
      filterType: 'Filter by type', filterStatus: 'Filter by review status', pair1: 'provision pair', pairN: 'provision pairs', amended: 'Text since amended',
      noMatch: 'No questions match', noMatchText: 'Try a different search word or clear the filters.', clear: 'Clear filters',
      provisions: 'Provisions', provision: 'Provision', sideBySide: 'Side by side', example: 'Example situation', reasoning: 'Reasoning',
      whyIssue: 'Why this may be an issue', whyNot: 'Why it may not be', definitionOf: 'Definition of', showFull: 'Show full text', showLess: 'Show less',
      highlighted: 'Highlighted', highlightedText: ' is the passage the system relied on.', textOf: 'Text of provision',
      checkText: 'Check the current text. ', checkTextBody: 'A later amendment changed or repealed a provision shown here.',
      notYet: 'Not available for this finding yet.', questionN: 'Question {a} of {b}', pairsInQuestion: 'Provision pairs in this question', sections: 'Sections',
      decidedSeparately: 'Decided separately', yourDecision: 'Your decision', appliesTo: 'Applies to', wholeQuestion: 'The whole question (all {a} provision pairs)',
      onlyPair: 'Only pair {a}: {b}', covered: 'Currently covered by the decision for the whole question: ', acceptOrDecline: 'Accept or decline',
      accept: 'Accept', decline: 'Decline', realIssue: 'A real issue', notIssue: 'Not an issue', whyDecline: 'Why are you declining?', chooseReason: 'Choose a reason…',
      justification: 'Your justification', note: 'Note', required: ' (required)', optional: ' (optional)', save: 'Save decision', saveAccept: 'Save: accept', saveDecline: 'Save: decline',
      saving: 'Saving…', cancel: 'Cancel', errChoose: 'Choose Accept or Decline first.', errReason: 'Choose a reason for declining.', errJust: 'Explain why you are declining. One sentence is enough.',
      errSave: 'Could not save. Check that the review server is still running.', reason: 'Reason: ', edit: 'Edit decision', remove: 'Remove decision',
      removeConfirm: 'Remove this decision? The question goes back to “To review”. The removed decision stays in the log.', canEdit: 'You can edit or remove this decision at any time.',
      nextToReview: 'Next question to review', backToList: 'Back to the list', prev: '← Previous', next: 'Next →', savedAccept: 'Saved: accepted', savedDecline: 'Saved: declined', removed: 'Decision removed',
      discard: 'You have typed a justification that is not saved yet. Leave this page and discard it?', discardPair: 'Discard the justification you have typed?',
      kAccept: 'Choose Accept', kDecline: 'Choose Decline', kNext: 'Next question', kPrev: 'Previous question', kSearch: 'Search the list', kHelp: 'Show this help', or: 'or', close: 'Close',
      loadErr: 'The review data could not be loaded', loadErr2: 'Start the review server and reload this page.', breadcrumb: 'Breadcrumb', prevNext: 'Previous and next question',
      r_not_a_conflict: 'Not a real problem', r_wrong_provisions: 'Right idea, wrong provisions', r_outdated_text: 'The law text shown is out of date', r_needs_context: 'Cannot be judged without more context', r_other: 'Other'
    },
    nl: {
      brand1: 'Wetgevings­review', brand2: '', title: 'Wetgevingsreview', skip: 'Naar de inhoud', language: 'Taal',
      overview: 'Overzicht', soon: 'Binnenkort', of: 'van', questionsReviewed: 'vragen beoordeeld', reviewingAs: 'Beoordelaar',
      yourName: 'Uw naam…', shortcuts: 'Sneltoetsen', openNav: 'Navigatie openen', categories: 'Categorieën',
      eyebrow: 'Nederlands en Europees digitaal recht', heroTitle: 'Het systeem vond de aanknopingspunten. U bepaalt welke ertoe doen.',
      heroText: 'Deze werkomgeving heeft tien wetten over data, cyberbeveiliging en AI gelezen en plaatsen gemarkeerd waar twee bepalingen kunnen botsen of elkaar herhalen. Elk punt is een aanknopingspunt voor een jurist, geen juridische conclusie.',
      start: 'Begin met beoordelen', cont: 'Ga verder met beoordelen', how: 'Zo werkt het', toReview: 'Te beoordelen', accepted: 'Geaccepteerd', declined: 'Afgewezen',
      statLaws: 'wetten gelezen', statQuestions: 'vragen om te beoordelen', statPairs: 'paren van bepalingen daarachter', statDone: 'tot nu toe beoordeeld',
      stepOf: 'Stap {a} van 4',
      s1: 'Kies een categorie', s1t: 'Elke categorie bevat één soort bevinding, verdeeld in kleinere groepen. De balken op de categoriekaarten laten zien hoeveel vragen elke groep heeft. Klik op een balk om er direct naartoe te gaan.',
      s2: 'Open een vraag', s2t: 'Elke vraag heeft dezelfde vier onderdelen: beide bepalingen volledig met de kernpassage gemarkeerd, een vergelijking naast elkaar, een voorbeeldsituatie, en de redenering voor en tegen.',
      s3: 'Accepteer of wijs af', s3t: 'Accepteer als het een echt punt is dat aandacht verdient. Wijs af als dat niet zo is, of als het systeem de wet verkeerd heeft gelezen. Het beslispaneel blijft in beeld terwijl u leest, en A en D werken als sneltoets.',
      s4: 'Leg uit waarom u afwijst', s4t: 'Bij elke afwijzing zijn een reden en een korte motivering verplicht. Van die geschreven redenen leert het systeem. U kunt elke beslissing later bewerken of verwijderen.',
      against: 'tegenover', reasonRequired: 'Reden verplicht', notAvailable: 'Nog niet beschikbaar', open: 'Open {a}', goodToKnow: 'Goed om te weten',
      f1q: 'Hoe zijn de vragen geordend?', f1a: 'Op hoe goed ze onderbouwd zijn. Vier balkjes betekent dat het verschil direct uit de tekst is berekend, zonder AI-oordeel. Drie balkjes betekent dat twee onafhankelijke AI-controles het eens waren. Twee balkjes betekent dat één AI-controle ernaar heeft gekeken.',
      f2q: 'Waarom omvat één vraag meerdere paren van bepalingen?', f2a: 'Twee lange artikelen kunnen lid voor lid dezelfde vraag oproepen. In plaats van die dertien keer te tonen, groepeert de werkomgeving ze tot één vraag. U beslist één keer en kunt een afzonderlijk paar apart beoordelen als dat afwijkt.',
      f3q: 'In welke taal is alles?', f3a: 'Wettekst wordt altijd in het oorspronkelijke Nederlands getoond. De interface en alle toelichtingen volgen de taalkeuze rechtsboven.',
      f4q: 'Kan ik een beslissing wijzigen?', f4a: 'Ja, altijd. Open de vraag en kies Beslissing bewerken of Beslissing verwijderen. Eerdere versies blijven in een logboek bewaard, dus er gaat niets verloren.',
      nAccepted: 'geaccepteerd', nDeclined: 'afgewezen', nToReview: 'te beoordelen', search: 'Zoek een wet of onderwerp…', searchLabel: 'Vragen doorzoeken', sortLabel: 'Vragen sorteren',
      sortSupport: 'Best onderbouwd eerst', sortPairs: 'Meeste paren eerst', sortLaw: 'Op naam van de wet', all: 'Alle', allTypes: 'Alle soorten',
      filterType: 'Filter op soort', filterStatus: 'Filter op beoordelingsstatus', pair1: 'paar bepalingen', pairN: 'paren bepalingen', amended: 'Tekst inmiddels gewijzigd',
      noMatch: 'Geen vragen gevonden', noMatchText: 'Probeer een ander zoekwoord of wis de filters.', clear: 'Filters wissen',
      provisions: 'Bepalingen', provision: 'Bepaling', sideBySide: 'Naast elkaar', example: 'Voorbeeldsituatie', reasoning: 'Redenering',
      whyIssue: 'Waarom dit een probleem kan zijn', whyNot: 'Waarom mogelijk niet', definitionOf: 'Definitie van', showFull: 'Toon volledige tekst', showLess: 'Toon minder',
      highlighted: 'Gemarkeerd', highlightedText: ' is de passage waarop het systeem zich baseerde.', textOf: 'Tekst van bepaling',
      checkText: 'Controleer de geldende tekst. ', checkTextBody: 'Een latere wijziging heeft een hier getoonde bepaling gewijzigd of ingetrokken.',
      notYet: 'Nog niet beschikbaar voor deze bevinding.', questionN: 'Vraag {a} van {b}', pairsInQuestion: 'Paren van bepalingen in deze vraag', sections: 'Onderdelen',
      decidedSeparately: 'Apart beoordeeld', yourDecision: 'Uw beslissing', appliesTo: 'Geldt voor', wholeQuestion: 'De hele vraag (alle {a} paren van bepalingen)',
      onlyPair: 'Alleen paar {a}: {b}', covered: 'Valt nu onder de beslissing voor de hele vraag: ', acceptOrDecline: 'Accepteren of afwijzen',
      accept: 'Accepteren', decline: 'Afwijzen', realIssue: 'Een echt punt', notIssue: 'Geen probleem', whyDecline: 'Waarom wijst u af?', chooseReason: 'Kies een reden…',
      justification: 'Uw motivering', note: 'Notitie', required: ' (verplicht)', optional: ' (optioneel)', save: 'Beslissing opslaan', saveAccept: 'Opslaan: accepteren', saveDecline: 'Opslaan: afwijzen',
      saving: 'Opslaan…', cancel: 'Annuleren', errChoose: 'Kies eerst Accepteren of Afwijzen.', errReason: 'Kies een reden voor de afwijzing.', errJust: 'Leg uit waarom u afwijst. Eén zin is genoeg.',
      errSave: 'Opslaan is niet gelukt. Controleer of de reviewserver nog draait.', reason: 'Reden: ', edit: 'Beslissing bewerken', remove: 'Beslissing verwijderen',
      removeConfirm: 'Deze beslissing verwijderen? De vraag gaat terug naar “Te beoordelen”. De verwijderde beslissing blijft in het logboek staan.', canEdit: 'U kunt deze beslissing altijd bewerken of verwijderen.',
      nextToReview: 'Volgende vraag om te beoordelen', backToList: 'Terug naar de lijst', prev: '← Vorige', next: 'Volgende →', savedAccept: 'Opgeslagen: geaccepteerd', savedDecline: 'Opgeslagen: afgewezen', removed: 'Beslissing verwijderd',
      discard: 'U heeft een motivering getypt die nog niet is opgeslagen. Deze pagina verlaten en de tekst weggooien?', discardPair: 'De getypte motivering weggooien?',
      kAccept: 'Kies Accepteren', kDecline: 'Kies Afwijzen', kNext: 'Volgende vraag', kPrev: 'Vorige vraag', kSearch: 'Zoek in de lijst', kHelp: 'Toon deze hulp', or: 'of', close: 'Sluiten',
      loadErr: 'De reviewgegevens konden niet worden geladen', loadErr2: 'Start de reviewserver en laad deze pagina opnieuw.', breadcrumb: 'Kruimelpad', prevNext: 'Vorige en volgende vraag',
      r_not_a_conflict: 'Geen echt probleem', r_wrong_provisions: 'Goed idee, verkeerde bepalingen', r_outdated_text: 'De getoonde wettekst is verouderd', r_needs_context: 'Niet te beoordelen zonder meer context', r_other: 'Anders'
    }
  };
  var REASON_IDS = ['not_a_conflict', 'wrong_provisions', 'outdated_text', 'needs_context', 'other'];
  var MIN_JUSTIFICATION = 10;
  var REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  var DATA = null, DECISIONS = {};
  var LANG = (function () {
    try { var s = localStorage.getItem('lang'); if (s === 'nl' || s === 'en') return s; } catch (e) { /* ignore */ }
    return (navigator.language || 'nl').toLowerCase().indexOf('nl') === 0 ? 'nl' : 'en';
  })();

  var main = document.getElementById('main'), nav = document.getElementById('nav'), sidefoot = document.getElementById('sidefoot');
  var side = document.getElementById('side'), scrim = document.getElementById('scrim'), menuBtn = document.getElementById('menu');
  var toasts = document.getElementById('toasts'), keysDialog = document.getElementById('keys'), langBox = document.getElementById('lang');

  // State that must survive a language switch (a re-render of the same screen).
  var drafts = {};                 // target id -> { choice, reason, text, editing }
  var qview = { id: null, i: 0, scope: 'question' };
  var ui = { step: 0, faq: {}, full: {} };
  var currentHash = location.hash;
  var page = { kind: 'overview' };
  var spy = null;

  // ---------- helpers ----------
  var SVGNS = 'http://www.w3.org/2000/svg';
  function t(key, a, b) { return (STR[LANG][key] || STR.en[key] || key).replace('{a}', a).replace('{b}', b); }
  function L(v) { return v && typeof v === 'object' ? (v[LANG] || v.en || v.nl || '') : (v || ''); }
  function h(tag, attrs) {
    var el = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k.slice(0, 2) === 'on') el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? '' : v);
    });
    for (var i = 2; i < arguments.length; i++) append(el, arguments[i]);
    return el;
  }
  function s(tag, attrs) {
    var el = document.createElementNS(SVGNS, tag);
    if (attrs) Object.keys(attrs).forEach(function (k) { el.setAttribute(k, attrs[k]); });
    for (var i = 2; i < arguments.length; i++) el.appendChild(arguments[i]);
    return el;
  }
  function append(el, child) {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) child.forEach(function (c) { append(el, c); });
    else el.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
  }
  function reviewer() { try { return localStorage.getItem('reviewer') || ''; } catch (e) { return ''; } }
  function setReviewer(v) { try { localStorage.setItem('reviewer', v); } catch (e) { /* private mode */ } }
  function category(id) { return DATA.categories.filter(function (c) { return c.id === id; })[0]; }
  function live() { return DATA.categories.filter(function (c) { return c.live; }); }
  function count(cat, kind) { return cat.questions.filter(function (q) { var d = DECISIONS[q.id]; return kind ? d && d.decision === kind : d; }).length; }
  function pairs(n) { return n + ' ' + (n === 1 ? t('pair1') : t('pairN')); }
  function fmtDate(iso) {
    try { return new Intl.DateTimeFormat(LANG === 'nl' ? 'nl-NL' : 'en-GB', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(iso)); } catch (e) { return iso; }
  }
  function hasUnsaved() {
    return Object.keys(drafts).some(function (k) {
      var txt = (drafts[k].text || '').trim(), saved = DECISIONS[k];
      return txt && !(saved && (saved.justification || '') === txt);
    });
  }
  window.addEventListener('beforeunload', function (ev) { if (hasUnsaved()) { ev.preventDefault(); ev.returnValue = ''; } });

  function toast(msg, kind) {
    var el = h('div', { class: 'toast ' + (kind || ''), text: msg });
    toasts.appendChild(el);
    setTimeout(function () { el.remove(); }, 3200);
  }
  function ring(pct, size, dark, still) {
    var r = (size - 10) / 2, c = 2 * Math.PI * r, animate = !REDUCED && !still;
    var val = s('circle', { class: 'val', cx: size / 2, cy: size / 2, r: r, 'stroke-dasharray': c, 'stroke-dashoffset': animate ? c : c * (1 - pct) });
    if (animate) requestAnimationFrame(function () { requestAnimationFrame(function () { val.setAttribute('stroke-dashoffset', c * (1 - pct)); }); });
    return s('svg', { class: 'ring' + (dark ? ' dark' : ''), width: size, height: size, viewBox: '0 0 ' + size + ' ' + size, 'aria-hidden': 'true' },
      s('circle', { class: 'track', cx: size / 2, cy: size / 2, r: r }), val);
  }
  function ringBox(pct, size, label, dark, still) { return h('div', { class: 'ringwrap' }, ring(pct, size, dark, still), h('b', { text: label })); }
  function meter(priority) {
    var on = { 1: 4, 2: 3, 3: 2, 4: 1, 5: 1 }[priority] || 2;
    var m = h('span', { class: 'meter', 'aria-hidden': 'true' });
    for (var i = 0; i < 4; i++) m.appendChild(h('i', { class: i < on ? 'on' : '' }));
    return m;
  }
  function countUp(el, n, still) {
    if (REDUCED || still || n === 0) { el.textContent = String(n); return; }
    var t0 = null;
    function step(ts) {
      if (!t0) t0 = ts;
      var p = Math.min(1, (ts - t0) / 700), e = 1 - Math.pow(1 - p, 3);
      el.textContent = String(Math.round(n * e));
      if (p < 1) requestAnimationFrame(step);
    }
    el.textContent = '0'; requestAnimationFrame(step);
  }
  function lawChip(p) { return h('span', { class: 'law' + (p.law_id.indexOf('BWBR') === 0 ? ' nl' : ''), text: L(p.law) }); }
  function statusPill(d) {
    return h('span', { class: 'pill ' + (d ? d.decision : 'open'), text: d ? (d.decision === 'accept' ? t('accepted') : t('declined')) : t('toReview') });
  }
  function postDecision(body) {
    return fetch('/api/decisions', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(t('errSave')); return j; }); },
        function () { throw new Error(t('errSave')); });
  }
  function nextOpen() {
    var cats = live();
    for (var i = 0; i < cats.length; i++) {
      var q = cats[i].questions.filter(function (x) { return !DECISIONS[x.id]; })[0];
      if (q) return '#/c/' + cats[i].id + '/' + q.id;
    }
    return '#/c/' + cats[0].id;
  }

  // ---------- chrome: language switch, sidebar, shortcuts dialog ----------
  function renderChrome() {
    document.documentElement.lang = LANG;
    document.querySelector('.skip').textContent = t('skip');
    menuBtn.setAttribute('aria-label', t('openNav'));
    nav.setAttribute('aria-label', t('categories'));
    var brand = document.querySelector('.brand span');
    brand.textContent = '';
    append(brand, t('brand2') ? [t('brand1'), h('br'), t('brand2')] : t('brand1'));
    document.querySelector('.mobilebrand').textContent = t('title');

    langBox.textContent = '';
    langBox.setAttribute('aria-label', t('language'));
    [['nl', 'Nederlands', 'NL'], ['en', 'English', 'EN']].forEach(function (x) {
      langBox.appendChild(h('button', { type: 'button', lang: x[0], 'aria-pressed': String(LANG === x[0]), title: x[1], 'aria-label': x[1], onclick: function () { setLang(x[0]); } }, x[2]));
    });

    keysDialog.textContent = '';
    function row(keys, label) { return [h('dt', null, keys), h('dd', { text: label })]; }
    function k(x) { return h('kbd', { text: x }); }
    append(keysDialog, [
      h('h2', { id: 'keys-title', text: t('shortcuts') }),
      h('dl', { class: 'keys' },
        row(k('A'), t('kAccept')), row(k('D'), t('kDecline')),
        row([k('J'), ' ' + t('or') + ' ', k('→')], t('kNext')), row([k('K'), ' ' + t('or') + ' ', k('←')], t('kPrev')),
        row(k('/'), t('kSearch')), row(k('?'), t('kHelp'))),
      h('form', { method: 'dialog' }, h('button', { class: 'btn primary', type: 'submit' }, t('close')))
    ]);
  }
  function setLang(l) {
    if (l === LANG) return;
    LANG = l;
    try { localStorage.setItem('lang', l); } catch (e) { /* ignore */ }
    var y = window.scrollY, focusId = document.activeElement && document.activeElement.id;
    render(true);
    window.scrollTo(0, y);
    var back = (focusId && document.getElementById(focusId)) || langBox.querySelector('[aria-pressed=true]');
    if (back) back.focus({ preventScroll: true });
  }

  function renderSide(activeCat, activeSub) {
    nav.textContent = '';
    append(nav, h('a', { class: 'nav-link', href: '#/', 'aria-current': activeCat ? null : 'page' }, t('overview')));
    DATA.categories.forEach(function (c) {
      if (!c.live) { append(nav, h('div', { class: 'nav-off' }, L(c.title), h('small', { text: t('soon') }))); return; }
      append(nav, h('a', { class: 'nav-link', href: '#/c/' + c.id, 'aria-current': c.id === activeCat && !activeSub ? 'page' : null },
        L(c.title), h('span', { class: 'n', text: count(c) + '/' + c.questions.length })));
      if (c.id === activeCat) append(nav, h('div', { class: 'nav-sub' }, c.subcategories.map(function (sc) {
        return h('a', { href: '#/c/' + c.id + '?type=' + sc.id, 'aria-current': sc.id === activeSub ? 'true' : null },
          L(sc.title), h('span', { class: 'n', text: String(sc.count) }));
      })));
    });
    var total = 0, done = 0;
    live().forEach(function (c) { total += c.questions.length; done += count(c); });
    var nameInput = h('input', { type: 'text', id: 'reviewer', name: 'reviewer', autocomplete: 'name', spellcheck: 'false', value: reviewer(),
      placeholder: t('yourName'), oninput: function () { setReviewer(nameInput.value.trim()); } });
    sidefoot.textContent = '';
    append(sidefoot, [
      h('div', { class: 'side-card' }, ringBox(total ? done / total : 0, 56, '', false, true),
        h('div', null, h('b', { class: 't', text: done + ' ' + t('of') + ' ' + total }), h('span', { text: t('questionsReviewed') }))),
      h('label', null, t('reviewingAs'), nameInput),
      h('button', { class: 'linkbtn', type: 'button', onclick: function () { keysDialog.showModal(); } }, t('shortcuts'))
    ]);
  }
  function closeMenu() { side.classList.remove('open'); scrim.hidden = true; menuBtn.setAttribute('aria-expanded', 'false'); }
  menuBtn.addEventListener('click', function () {
    var open = !side.classList.contains('open');
    side.classList.toggle('open', open); scrim.hidden = !open; menuBtn.setAttribute('aria-expanded', String(open));
  });
  scrim.addEventListener('click', closeMenu);

  // ---------- overview ----------
  function overviewScreen(still) {
    document.title = t('title');
    page = { kind: 'overview' };
    renderSide(null);
    var total = 0, done = 0, acc = 0, dec = 0, np = 0;
    live().forEach(function (c) {
      total += c.questions.length; done += count(c); acc += count(c, 'accept'); dec += count(c, 'decline');
      c.questions.forEach(function (q) { np += q.instances.length; });
    });

    var hero = h('section', { class: 'hero' },
      h('div', null,
        h('div', { class: 'eyebrow', text: t('eyebrow') }), h('h1', { text: t('heroTitle') }), h('p', { text: t('heroText') }),
        h('div', { class: 'actions' },
          h('a', { class: 'btn light', href: nextOpen() }, done ? t('cont') : t('start')),
          h('a', { class: 'btn ghost', href: '#how', onclick: function (ev) { ev.preventDefault(); document.getElementById('how').scrollIntoView(); } }, t('how')))),
      h('div', { class: 'hero-panel' },
        ringBox(total ? done / total : 0, 150, Math.round(100 * (total ? done / total : 0)) + '%', false, still),
        h('div', { class: 'trio' },
          h('div', null, h('b', { text: String(total - done) }), h('span', { text: t('toReview') })),
          h('div', null, h('b', { text: String(acc) }), h('span', { text: t('accepted') })),
          h('div', null, h('b', { text: String(dec) }), h('span', { text: t('declined') })))));

    var stats = h('div', { class: 'stats' }, [[10, 'statLaws'], [total, 'statQuestions'], [np, 'statPairs'], [done, 'statDone']].map(function (d) {
      var b = h('b'); countUp(b, d[0], still);
      return h('div', { class: 'card stat' }, b, h('span', { text: t(d[1]) }));
    }));

    var demos = [
      function () { var sc = live()[0].subcategories; return sc.slice(0, 2).map(function (x) { return h('span', { class: 'pill tag', text: L(x.title) }); }); },
      function () { return [h('span', { class: 'law', text: LANG === 'nl' ? 'AVG' : 'GDPR' }), h('span', { class: 'muted', text: t('against') }), h('span', { class: 'law nl', text: 'Cyberbeveiligingswet' })]; },
      function () { return [h('span', { class: 'pill accept', text: t('accepted') }), h('span', { class: 'pill decline', text: t('declined') })]; },
      function () { return [h('span', { class: 'pill', text: t('reasonRequired') })]; }
    ];
    var panel = h('div', { class: 'steppanel', role: 'tabpanel', id: 'steppanel', tabindex: '0' });
    var tabs = h('div', { class: 'steptabs', role: 'tablist', 'aria-label': t('how') });
    function showStep(i, focus) {
      ui.step = i;
      Array.prototype.forEach.call(tabs.children, function (b, j) { b.setAttribute('aria-selected', String(i === j)); b.tabIndex = i === j ? 0 : -1; });
      panel.textContent = '';
      append(panel, [h('div', { class: 'eyebrow', style: 'color:var(--blue-600)', text: t('stepOf', i + 1) }),
        h('h3', { style: 'font-size:1.3rem', text: t('s' + (i + 1)) }), h('p', { text: t('s' + (i + 1) + 't') }), h('div', { class: 'demo' }, demos[i]())]);
      if (focus) tabs.children[i].focus();
    }
    [0, 1, 2, 3].forEach(function (i) {
      tabs.appendChild(h('button', { type: 'button', role: 'tab', 'aria-controls': 'steppanel', onclick: function () { showStep(i); },
        onkeydown: function (ev) {
          if (ev.key === 'ArrowDown' || ev.key === 'ArrowRight') { ev.preventDefault(); showStep((i + 1) % 4, true); }
          if (ev.key === 'ArrowUp' || ev.key === 'ArrowLeft') { ev.preventDefault(); showStep((i + 3) % 4, true); }
        } }, h('span', { text: String(i + 1) }), t('s' + (i + 1))));
    });
    showStep(ui.step);

    var cats = h('div', { class: 'cats' }, DATA.categories.map(function (c) {
      if (!c.live) return h('div', { class: 'card cat off' },
        h('div', null, h('h3', { text: L(c.title) }), h('p', { class: 'muted', style: 'margin:6px 0 0', text: L(c.summary) })),
        h('div', null, h('span', { class: 'pill', text: t('notAvailable') })));
      var n = count(c), tot = c.questions.length, max = Math.max.apply(null, c.subcategories.map(function (x) { return x.count; }));
      return h('div', { class: 'card cat live' },
        h('div', { class: 'cat-top' }, ringBox(tot ? n / tot : 0, 72, n + '/' + tot, true, still),
          h('div', null, h('h3', { style: 'font-size:1.2rem', text: L(c.title) }), h('p', { text: L(c.summary) }))),
        h('div', { class: 'bars' + (still ? ' still' : '') }, c.subcategories.map(function (sc) {
          return h('a', { class: 'barrow', href: '#/c/' + c.id + '?type=' + sc.id, title: L(sc.description) },
            h('span', { text: L(sc.title) }), h('span', { class: 'tr' }, h('i', { style: 'width:' + Math.max(4, Math.round(100 * sc.count / max)) + '%' })),
            h('span', { class: 'v', text: String(sc.count) }));
        })),
        h('a', { class: 'btn primary', href: '#/c/' + c.id }, t('open', L(c.title).toLowerCase())));
    }));

    return [hero, stats,
      h('section', { class: 'section', id: 'how' }, h('h2', { text: t('how') }), h('div', { class: 'card stepper' }, tabs, panel)),
      h('section', { class: 'section' }, h('h2', { text: t('categories') }), cats),
      h('section', { class: 'section' }, h('h2', { text: t('goodToKnow') }),
        h('div', { class: 'card' }, [1, 2, 3, 4].map(function (i) {
          var d = h('details', { class: 'faq', open: !!ui.faq[i], ontoggle: function () { ui.faq[i] = d.open; } },
            h('summary', { text: t('f' + i + 'q') }), h('p', { text: t('f' + i + 'a') }));
          return d;
        })))];
  }

  // ---------- list ----------
  function listScreen(cat) {
    document.title = L(cat.title) + ' – ' + t('title');
    var qp = new URLSearchParams((location.hash.split('?')[1]) || '');
    var f = {
      sub: cat.subcategories.some(function (x) { return x.id === qp.get('type'); }) ? qp.get('type') : 'all',
      status: ['open', 'accept', 'decline'].indexOf(qp.get('status')) > -1 ? qp.get('status') : 'all',
      q: qp.get('q') || '',
      sort: ['support', 'pairs', 'law'].indexOf(qp.get('sort')) > -1 ? qp.get('sort') : 'support'
    };
    page = { kind: 'list' };
    renderSide(cat.id, f.sub === 'all' ? null : f.sub);

    function syncUrl() {
      var p = new URLSearchParams();
      if (f.sub !== 'all') p.set('type', f.sub);
      if (f.status !== 'all') p.set('status', f.status);
      if (f.q) p.set('q', f.q);
      if (f.sort !== 'support') p.set('sort', f.sort);
      var str = p.toString(); currentHash = '#/c/' + cat.id + (str ? '?' + str : '');
      history.replaceState(null, '', currentHash);
      renderSide(cat.id, f.sub === 'all' ? null : f.sub);
    }
    function matches(q) {
      if (f.sub !== 'all' && q.subcategory !== f.sub) return false;
      var d = DECISIONS[q.id];
      if (f.status === 'open' && d) return false;
      if ((f.status === 'accept' || f.status === 'decline') && !(d && d.decision === f.status)) return false;
      if (f.q) {
        var hay = (q.title.nl + ' ' + q.title.en + ' ' + L(q.summary)).toLowerCase();
        if (hay.indexOf(f.q.toLowerCase()) === -1) return false;
      }
      return true;
    }
    function sorted(qs) {
      var c = qs.slice();
      if (f.sort === 'pairs') c.sort(function (a, b) { return b.instances.length - a.instances.length; });
      else if (f.sort === 'law') c.sort(function (a, b) { return L(a.title).localeCompare(L(b.title)); });
      return c;
    }
    function highlight(text) {
      if (!f.q) return text;
      var i = text.toLowerCase().indexOf(f.q.toLowerCase());
      if (i === -1) return text;
      return [text.slice(0, i), h('mark', { text: text.slice(i, i + f.q.length) }), text.slice(i + f.q.length)];
    }

    var acc = count(cat, 'accept'), dec = count(cat, 'decline'), tot = cat.questions.length;
    var progress = h('div', { class: 'card progress' },
      h('div', { class: 'seg', role: 'img', 'aria-label': acc + ' ' + t('nAccepted') + ', ' + dec + ' ' + t('nDeclined') + ', ' + (tot - acc - dec) + ' ' + t('nToReview') },
        h('i', { class: 'a', style: 'width:' + (100 * acc / tot) + '%' }), h('i', { class: 'd', style: 'width:' + (100 * dec / tot) + '%' })),
      h('div', { class: 'legend' },
        h('span', { class: 'a' }, h('b', { text: String(acc) }), ' ' + t('nAccepted')), h('span', { class: 'd' }, h('b', { text: String(dec) }), ' ' + t('nDeclined')),
        h('span', null, h('b', { text: String(tot - acc - dec) }), ' ' + t('nToReview'))));

    var listWrap = h('div');
    var chips = h('div', { class: 'chips', role: 'group', 'aria-label': t('filterType') });
    var seg = h('div', { class: 'segctl', role: 'group', 'aria-label': t('filterStatus') });
    var search = h('input', { type: 'search', id: 'search', name: 'q', autocomplete: 'off', spellcheck: 'false', value: f.q,
      placeholder: t('search'), 'aria-label': t('searchLabel'), oninput: function () { f.q = search.value.trim(); syncUrl(); drawList(); } });
    var sortSel = h('select', { id: 'sort', name: 'sort', 'aria-label': t('sortLabel'), onchange: function () { f.sort = sortSel.value; syncUrl(); drawList(); } },
      h('option', { value: 'support', text: t('sortSupport') }), h('option', { value: 'pairs', text: t('sortPairs') }), h('option', { value: 'law', text: t('sortLaw') }));
    sortSel.value = f.sort;

    function drawFilters() {
      chips.textContent = ''; seg.textContent = '';
      function chip(value, label, n) {
        return h('button', { class: 'chip', type: 'button', 'aria-pressed': String(f.sub === value),
          onclick: function () { f.sub = value; syncUrl(); drawFilters(); drawList(); } }, label, h('span', { class: 'n', text: String(n) }));
      }
      append(chips, chip('all', t('allTypes'), cat.questions.length));
      cat.subcategories.forEach(function (sc) { append(chips, chip(sc.id, L(sc.title), sc.count)); });
      [['all', t('all')], ['open', t('toReview')], ['accept', t('accepted')], ['decline', t('declined')]].forEach(function (x) {
        append(seg, h('button', { type: 'button', 'aria-pressed': String(f.status === x[0]),
          onclick: function () { f.status = x[0]; syncUrl(); drawFilters(); drawList(); } }, x[1]));
      });
    }
    function drawList() {
      listWrap.textContent = '';
      var shown = 0;
      cat.subcategories.forEach(function (sc) {
        var qs = sorted(cat.questions.filter(function (q) { return q.subcategory === sc.id && matches(q); }));
        if (!qs.length) return;
        shown += qs.length;
        append(listWrap, h('section', { class: 'group' },
          h('h2', null, L(sc.title), ' ', h('span', { class: 'muted', style: 'font-weight:500', text: '(' + qs.length + ')' })), h('p', { text: L(sc.description) }),
          h('ul', { class: 'qlist' }, qs.map(function (q) {
            var d = DECISIONS[q.id], n = q.instances.length, i0 = q.instances[0], sum = L(q.summary);
            return h('li', { class: 'q ' + (d ? d.decision : '') }, h('a', { class: 'card', href: '#/c/' + cat.id + '/' + q.id },
              h('span', { class: 'laws' }, lawChip(i0.a), h('span', { class: 'x', text: 'art. ' + i0.a.article }), h('span', { class: 'x', text: '↔' }),
                lawChip(i0.b), h('span', { class: 'x', text: 'art. ' + i0.b.article })),
              h('span', { class: 'st' }, statusPill(d), h('span', { class: 'arrow', 'aria-hidden': 'true', text: '→' })),
              h('span', { class: 't' }, highlight(L(q.title))),
              sum ? h('span', { class: 's' }, highlight(sum)) : null,
              h('span', { class: 'm' },
                h('span', null, meter(q.priority), ' ' + L(q.priority_label)), h('span', { text: pairs(n) }),
                q.amended ? h('span', { class: 'pill warn', text: t('amended') }) : null)));
          }))));
      });
      if (!shown) append(listWrap, h('div', { class: 'card empty' }, h('h3', { text: t('noMatch') }),
        h('p', { style: 'margin:8px 0 16px', text: t('noMatchText') }),
        h('button', { class: 'btn', type: 'button', onclick: function () { f.sub = 'all'; f.status = 'all'; f.q = ''; search.value = ''; syncUrl(); drawFilters(); drawList(); } }, t('clear'))));
    }
    drawFilters(); drawList();

    return [
      h('nav', { class: 'crumbs', 'aria-label': t('breadcrumb') }, h('a', { href: '#/' }, t('overview')), ' / ' + L(cat.title)),
      h('div', { class: 'pagehead' }, h('h1', { text: L(cat.title) }), h('p', { text: L(cat.summary) })),
      progress,
      h('div', { class: 'toolbar' },
        h('div', { class: 'search' },
          s('svg', { width: 18, height: 18, viewBox: '0 0 24 24', 'aria-hidden': 'true' },
            s('circle', { cx: 11, cy: 11, r: 7, fill: 'none', stroke: 'currentColor', 'stroke-width': 2 }),
            s('path', { d: 'M20 20l-3.5-3.5', stroke: 'currentColor', 'stroke-width': 2, 'stroke-linecap': 'round' })),
          search),
        sortSel, seg),
      chips, listWrap
    ];
  }

  // ---------- question ----------
  function provisionCard(sideName, p, key) {
    var tx = h('div', { class: 'tx' + (ui.full[key] ? ' full' : ''), lang: 'nl', tabindex: '0', role: 'group', 'aria-label': t('textOf') + ' ' + sideName });
    var kp = p.key_passage, i = kp && p.kind === 'paragraph' ? p.text.toLowerCase().indexOf(kp.toLowerCase()) : -1;
    var marked = i > -1 && kp.length < p.text.length;
    if (marked) append(tx, [p.text.slice(0, i), h('mark', { text: p.text.slice(i, i + kp.length) }), p.text.slice(i + kp.length)]);
    else tx.textContent = p.text;
    var title = p.kind === 'definition' && p.term ? t('definitionOf') + ' ‘' + p.term + '’' : L(p.law) + ' ' + p.ref;
    var toggle = p.text.length > 700 ? h('button', { class: 'btn', type: 'button', 'aria-expanded': String(!!ui.full[key]), onclick: function () {
      ui.full[key] = tx.classList.toggle('full'); toggle.textContent = ui.full[key] ? t('showLess') : t('showFull'); toggle.setAttribute('aria-expanded', String(ui.full[key]));
    } }, ui.full[key] ? t('showLess') : t('showFull')) : null;
    return h('article', { class: 'card prov' },
      h('div', { class: 'who' }, h('b', { text: t('provision') + ' ' + sideName }), lawChip(p)),
      h('h3', { text: title }),
      h('div', { class: 'hd', lang: 'nl', text: p.kind === 'definition' ? L(p.law) + ' ' + p.ref : (p.heading || '') }),
      tx, toggle,
      marked ? h('div', { class: 'hint' }, h('mark', { text: t('highlighted') }), t('highlightedText')) : null);
  }
  function para(v) { var x = L(v); return x ? h('p', { text: x }) : h('p', { class: 'muted', text: t('notYet') }); }

  // The same four sections for every finding, whatever detector produced it.
  function instanceBlocks(inst) {
    return [
      ['provisions', t('provisions'), [
        h('div', { class: 'pair' }, provisionCard('A', inst.a, inst.id + ':a'), provisionCard('B', inst.b, inst.id + ':b')),
        inst.currency_warning ? h('div', { class: 'callout', role: 'note' }, h('b', { text: t('checkText') }), t('checkTextBody')) : null]],
      ['compare', t('sideBySide'), h('div', { class: 'card tablewrap' }, h('table', { class: 'cmp' },
        h('thead', null, h('tr', null, h('th', { scope: 'col' }), h('th', { scope: 'col', text: t('provision') + ' A' }), h('th', { scope: 'col', text: t('provision') + ' B' }))),
        h('tbody', null, inst.facts.map(function (f) {
          var a = L(f.a), b = L(f.b);
          return h('tr', { class: f.compare && a !== b ? 'diff' : '' }, h('th', { scope: 'row', text: L(f.label) }), h('td', { text: a }), h('td', { text: b }));
        }))))],
      ['example', t('example'), h('div', { class: 'card scenario' }, para(inst.scenario))],
      ['reasoning', t('reasoning'), [
        h('div', { class: 'cases' },
          h('div', { class: 'card case for' }, h('h3', { text: t('whyIssue') }), para(inst.case_for)),
          h('div', { class: 'card case against' }, h('h3', { text: t('whyNot') }), para(inst.case_against))),
        inst.notes && inst.notes.length ? h('div', { class: 'card', style: 'margin-top:16px' }, h('dl', { class: 'kv' }, inst.notes.map(function (n) {
          return [h('dt', { text: L(n.label) }), h('dd', { text: L(n.value) })];
        }))) : null]]
    ];
  }

  function questionScreen(cat, q) {
    document.title = L(q.title).replace(/ /g, ' ') + ' – ' + t('title');
    renderSide(cat.id, q.subcategory);
    if (qview.id !== q.id) qview = { id: q.id, i: 0, scope: 'question' };
    var state = qview;
    var sub = cat.subcategories.filter(function (x) { return x.id === q.subcategory; })[0];
    var idx = cat.questions.indexOf(q), prev = cat.questions[idx - 1], next = cat.questions[idx + 1];
    var base = '#/c/' + cat.id + '/';
    var left = h('div'), right = h('div'), tabsWrap = h('div');
    page = { kind: 'question', prev: prev ? base + prev.id : null, next: next ? base + next.id : null,
      choose: function (v) { var r = right.querySelector('.choice input[value="' + v + '"]'); if (r) { r.checked = true; r.dispatchEvent(new Event('change', { bubbles: true })); r.focus(); } } };

    function drawTabs() {
      tabsWrap.textContent = '';
      if (q.instances.length < 2) return;
      append(tabsWrap, h('div', { class: 'pairtabs', role: 'tablist', 'aria-label': t('pairsInQuestion') }, q.instances.map(function (inst, i) {
        var d = DECISIONS[inst.id];
        return h('button', { type: 'button', role: 'tab', 'aria-selected': String(i === state.i), onclick: function () {
          if (hasUnsaved() && !window.confirm(t('discardPair'))) return;
          drafts = {}; state.i = i; drawTabs(); drawLeft(); drawDecision();
        } }, (i + 1) + '. ' + inst.a.ref + ' ↔ ' + inst.b.ref, d ? h('span', { class: 'dot ' + d.decision, title: t('decidedSeparately') }) : null);
      })));
    }
    function drawLeft() {
      if (spy) { spy.disconnect(); spy = null; }
      left.textContent = '';
      var blocks = instanceBlocks(q.instances[state.i]);
      var navEl = h('nav', { class: 'secnav', 'aria-label': t('sections') }, blocks.map(function (b) {
        return h('a', { href: '#sec-' + b[0], 'data-for': b[0], onclick: function (ev) { ev.preventDefault(); document.getElementById('sec-' + b[0]).scrollIntoView(); } }, b[1]);
      }));
      append(left, navEl);
      blocks.forEach(function (b) { append(left, h('section', { class: 'block', id: 'sec-' + b[0] }, h('h2', { text: b[1] }), b[2])); });
      if ('IntersectionObserver' in window) {
        spy = new IntersectionObserver(function (entries) {
          entries.forEach(function (e) {
            if (!e.isIntersecting) return;
            Array.prototype.forEach.call(navEl.children, function (a) { a.classList.toggle('on', 'sec-' + a.getAttribute('data-for') === e.target.id); });
          });
        }, { rootMargin: '-15% 0px -70% 0px' });
        Array.prototype.forEach.call(left.querySelectorAll('.block'), function (el) { spy.observe(el); });
      }
    }

    function drawDecision() {
      right.textContent = '';
      var inst = q.instances[state.i], multi = q.instances.length > 1;
      var targetId = state.scope === 'pair' ? inst.id : q.id;
      var d = DECISIONS[targetId], draft = drafts[targetId];
      var box = h('aside', { class: 'card decide', 'aria-label': t('yourDecision') }, h('h2', { text: t('yourDecision') }));
      if (multi) {
        var sname = 'scope-' + q.id;
        var setScope = function (v) { return function () { state.scope = v; drawDecision(); }; };
        append(box, h('fieldset', { class: 'scope' }, h('legend', { text: t('appliesTo') }),
          h('label', null, h('input', { type: 'radio', name: sname, value: 'question', checked: state.scope === 'question', onchange: setScope('question') }),
            h('span', { text: t('wholeQuestion', q.instances.length) })),
          h('label', null, h('input', { type: 'radio', name: sname, value: 'pair', checked: state.scope === 'pair', onchange: setScope('pair') }),
            h('span', { text: t('onlyPair', state.i + 1, inst.a.ref + ' ↔ ' + inst.b.ref) }))));
      }
      if (state.scope === 'pair' && !d && DECISIONS[q.id]) append(box, h('div', { class: 'covered' },
        t('covered'), h('b', { text: DECISIONS[q.id].decision === 'accept' ? t('nAccepted') : t('nDeclined') }), '.'));

      if (d && !(draft && draft.editing)) {
        var open = cat.questions.filter(function (x, i) { return i > idx && !DECISIONS[x.id]; })[0];
        append(box, h('div', { class: 'saved' },
          h('div', { class: 'big ' + d.decision }, d.decision === 'accept' ? t('accepted') : t('declined')),
          h('div', { class: 'muted', style: 'font-size:.88rem', text: (d.reviewer ? d.reviewer + ', ' : '') + fmtDate(d.decided_at) }),
          d.reason ? h('div', null, h('b', { text: t('reason') }), t('r_' + d.reason)) : null,
          d.justification ? h('blockquote', { text: d.justification }) : null,
          h('a', { class: 'btn primary', href: open ? base + open.id : '#/c/' + cat.id }, open ? t('nextToReview') : t('backToList')),
          h('div', { class: 'editrow' },
            h('button', { class: 'btn', type: 'button', id: 'edit-decision', onclick: function () {
              drafts[targetId] = { editing: true, choice: d.decision, reason: d.reason || '', text: d.justification || '' };
              drawDecision(); var r = right.querySelector('.choice input:checked'); if (r) r.focus();
            } }, t('edit')),
            h('button', { class: 'btn danger', type: 'button', onclick: function () {
              if (!window.confirm(t('removeConfirm'))) return;
              postDecision({ id: targetId, decision: null }).then(function () {
                delete DECISIONS[targetId]; delete drafts[targetId]; toast(t('removed'));
                renderSide(cat.id, q.subcategory); drawTabs(); drawDecision();
              }).catch(function (e) { toast(e.message, 'decline'); });
            } }, t('remove'))),
          h('p', { class: 'hint', style: 'margin:0', text: t('canEdit') })));
        append(right, box); return;
      }

      draft = draft || (drafts[targetId] = { choice: null, reason: '', text: '' });
      var name = 'd-' + targetId;
      var errEl = h('div', { class: 'err', id: name + '-err', role: 'alert' });
      var reasonSel = h('select', { id: name + '-reason', name: 'reason', 'aria-describedby': name + '-err', onchange: function () { draft.reason = reasonSel.value; } },
        h('option', { value: '', text: t('chooseReason') }), REASON_IDS.map(function (r) { return h('option', { value: r, text: t('r_' + r) }); }));
      var text = h('textarea', { id: name + '-just', name: 'justification', autocomplete: 'off', 'aria-describedby': name + '-err', oninput: function () { draft.text = text.value; } });
      var textLabel = h('label', { for: name + '-just' });
      var reasonWrap = h('div', null, h('label', { for: name + '-reason', text: t('whyDecline') }), reasonSel);
      var fields = h('div', { class: 'fields', hidden: true }, reasonWrap, h('div', null, textLabel, text));
      var save = h('button', { class: 'btn primary', type: 'submit' }, t('save'));
      function sync() {
        var c = draft.choice;
        fields.hidden = !c; reasonWrap.hidden = c !== 'decline'; errEl.textContent = '';
        reasonSel.removeAttribute('aria-invalid'); text.removeAttribute('aria-invalid');
        textLabel.textContent = '';
        append(textLabel, [c === 'decline' ? t('justification') : t('note'), h('span', { class: 'hint', text: c === 'decline' ? t('required') : t('optional') })]);
        save.textContent = c === 'decline' ? t('saveDecline') : c === 'accept' ? t('saveAccept') : t('save');
      }
      function opt(value, cls, label, subText, key) {
        return h('label', { class: cls }, h('input', { type: 'radio', name: name, value: value, checked: draft.choice === value,
          onchange: function () { draft.choice = value; sync(); } }), h('kbd', { 'aria-hidden': 'true', text: key }), label, h('small', { text: subText }));
      }
      var form = h('form', { novalidate: true, onsubmit: function (ev) {
        ev.preventDefault();
        var c = draft.choice, just = text.value.trim();
        if (!c) { errEl.textContent = t('errChoose'); form.querySelector('.choice input').focus(); return; }
        if (c === 'decline' && !reasonSel.value) { errEl.textContent = t('errReason'); reasonSel.setAttribute('aria-invalid', 'true'); reasonSel.focus(); return; }
        if (c === 'decline' && just.length < MIN_JUSTIFICATION) { errEl.textContent = t('errJust'); text.setAttribute('aria-invalid', 'true'); text.focus(); return; }
        save.disabled = true; save.textContent = t('saving');
        postDecision({ id: targetId, question_id: q.id, decision: c, reason: c === 'decline' ? reasonSel.value : null, justification: just, reviewer: reviewer() })
          .then(function (res) {
            DECISIONS[targetId] = res.entry; delete drafts[targetId];
            toast(c === 'accept' ? t('savedAccept') : t('savedDecline'), c);
            renderSide(cat.id, q.subcategory); drawTabs(); drawDecision();
            var b = right.querySelector('a.btn.primary'); if (b) b.focus();
          })
          .catch(function (e) { save.disabled = false; sync(); errEl.textContent = e.message; });
      } },
        h('fieldset', { class: 'choicewrap' }, h('legend', { class: 'vh', text: t('acceptOrDecline') }),
          h('div', { class: 'choice' }, opt('accept', 'acc', t('accept'), t('realIssue'), 'A'), opt('decline', 'dec', t('decline'), t('notIssue'), 'D'))),
        fields, errEl, save,
        d ? h('button', { class: 'btn quiet', type: 'button', onclick: function () { delete drafts[targetId]; drawDecision(); } }, t('cancel')) : null);
      reasonSel.value = draft.reason || ''; text.value = draft.text || '';
      sync();
      append(box, form);
      append(right, box);
    }

    drawTabs(); drawLeft(); drawDecision();
    return [
      h('nav', { class: 'crumbs', 'aria-label': t('breadcrumb') }, h('a', { href: '#/' }, t('overview')), ' / ', h('a', { href: '#/c/' + cat.id }, L(cat.title)), ' / ',
        h('a', { href: '#/c/' + cat.id + '?type=' + sub.id }, L(sub.title))),
      h('header', { class: 'qhero' },
        h('span', { class: 'pill tag', text: L(sub.title) }),
        h('h1', { text: L(q.title) }),
        L(q.summary) ? h('p', { class: 'sum', text: L(q.summary) }) : null,
        h('div', { class: 'qmeta' },
          h('span', null, t('questionN', idx + 1, cat.questions.length)), h('span', { class: 'posbar', 'aria-hidden': 'true' }, h('i', { style: 'width:' + (100 * (idx + 1) / cat.questions.length) + '%' })),
          h('span', null, meter(q.priority), ' ' + L(q.priority_label)), h('span', { text: pairs(q.instances.length) })),
        tabsWrap),
      h('div', { class: 'qgrid' }, left, right),
      h('nav', { class: 'pager', 'aria-label': t('prevNext') },
        prev ? h('a', { class: 'btn', href: base + prev.id }, t('prev')) : h('span'),
        h('a', { class: 'btn quiet', href: '#/c/' + cat.id }, t('backToList')),
        next ? h('a', { class: 'btn', href: base + next.id }, t('next')) : h('span'))
    ];
  }

  // ---------- keyboard ----------
  document.addEventListener('keydown', function (ev) {
    if (ev.ctrlKey || ev.metaKey || ev.altKey || keysDialog.open) return;
    var el = ev.target, tag = el.tagName;
    if (tag === 'TEXTAREA' || tag === 'SELECT' || (tag === 'INPUT' && el.type !== 'radio')) return;
    var k = ev.key, arrowsFree = tag !== 'INPUT' && el.getAttribute('role') !== 'tab';
    if (k === '?') { ev.preventDefault(); keysDialog.showModal(); return; }
    if (k === '/' && page.kind === 'list') { ev.preventDefault(); document.getElementById('search').focus(); return; }
    if (page.kind !== 'question') return;
    if (k === 'a' || k === 'A') { ev.preventDefault(); page.choose('accept'); }
    else if (k === 'd' || k === 'D') { ev.preventDefault(); page.choose('decline'); }
    else if ((k === 'j' || k === 'J' || (k === 'ArrowRight' && arrowsFree)) && page.next) location.hash = page.next;
    else if ((k === 'k' || k === 'K' || (k === 'ArrowLeft' && arrowsFree)) && page.prev) location.hash = page.prev;
  });

  // ---------- render and route ----------
  function render(still) {
    if (spy) { spy.disconnect(); spy = null; }
    renderChrome();
    var parts = location.hash.replace(/^#\/?/, '').split('?')[0].split('/').filter(Boolean);
    var view;
    if (parts[0] === 'c' && parts[1]) {
      var cat = category(parts[1]);
      if (cat && cat.live) {
        var q = parts[2] ? cat.questions.filter(function (x) { return x.id === parts[2]; })[0] : null;
        view = q ? questionScreen(cat, q) : listScreen(cat);
      }
    }
    if (!view) view = overviewScreen(still);
    main.textContent = '';
    main.classList.remove('enter');
    append(main, view);
    if (!still) { void main.offsetWidth; main.classList.add('enter'); }
  }
  function route() {
    if (hasUnsaved() && location.hash !== currentHash) {
      if (!window.confirm(t('discard'))) { history.replaceState(null, '', currentHash); return; }
    }
    if (location.hash.split('?')[0] !== currentHash.split('?')[0]) drafts = {};
    currentHash = location.hash;
    closeMenu();
    render(false);
    window.scrollTo(0, 0);
    main.focus({ preventScroll: true });
  }

  renderChromeSafe();
  function renderChromeSafe() { try { renderChrome(); } catch (e) { /* data not needed for chrome */ } }

  Promise.all([
    fetch('data/review_data.json').then(function (r) { if (!r.ok) throw new Error(); return r.json(); }),
    fetch('/api/decisions').then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
  ]).then(function (res) {
    DATA = res[0]; DECISIONS = res[1];
    window.addEventListener('hashchange', route);
    route();
  }).catch(function () {
    main.textContent = '';
    append(main, h('div', { class: 'card', style: 'padding:32px;max-width:640px' },
      h('h1', { style: 'font-size:1.6rem', text: t('loadErr') }),
      h('p', { style: 'margin-top:12px', text: t('loadErr2') }),
      h('p', { class: 'muted', text: 'python ui/build_review_data.py  /  python ui/server.py' })));
  });
})();
