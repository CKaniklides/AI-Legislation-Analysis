/* Laws in plain language: one more set of screens inside the review workspace.
   #/s                        the laws
   #/s/<law>                  the one-page sheet
   #/s/<law>/map              the stakeholder map
   #/s/<law>/chapters         the chapters
   #/s/<law>/ch/<n>           one chapter: its summary and a card per article
   #/s/<law>/doc              the public document (the version to share)
   #/s/<law>/doc/<n>          one chapter with its articles, as a document
   Every line can be approved, declined (a reason is required) or reworded, and every decision
   can be changed later. Legal text is always the original Dutch. Like the rest of the
   workspace, all data-derived text is inserted with textContent, never innerHTML. */
(function () {
  'use strict';
  var App = window.ReviewApp;
  if (!App) return;
  var h = App.h, s = App.s, append = App.append, t = App.t, L = App.L;

  var EN = {
    p_title: 'Laws in plain language', p_map_ok: 'approved', p_map_no: 'declined', p_map_mixed: 'partly reviewed', p_map_none: 'not reviewed yet', p_relCount: '{a} relations in this direction', p_bulk: 'All lines on this page', p_approveAll: 'Approve all ({a})', p_declineAll: 'Decline all ({a})', p_resetAll: 'Reset all ({a})', p_declineAllAsk: 'Decline all {a} lines on this page? No reason is needed. You can reset them afterwards.', p_resetAllAsk: 'Reset all {a} lines on this page to not reviewed? Decisions and rewordings on this page are removed.', p_declinedN: 'Declined {a} lines', p_resetN: 'Reset {a} lines', p_officialSource: 'Official text', p_nav: 'Simplification', p_cat: 'Each law brought down to what it asks, of whom, and why, in words anyone can read. You approve every line before it is shared.',
    p_lead: 'For each law the system writes what it comes down to: one page for the whole law, a short summary per chapter, and a few plain points per article. You approve, decline or reword every line. What you approve becomes a document the Ministry can share with the public.',
    p_h1: 'The system writes', p_h1t: 'from the legal text and from facts it computed: the terms, the duties, the exceptions.',
    p_h2: 'An automatic check', p_h2t: 'reads every line next to its article and marks what a reader could misread.',
    p_h3: 'You review', p_h3t: 'approve, decline with a reason, or reword. You can change a decision at any time.',
    p_h4: 'Share', p_h4t: 'the public document holds only what was not declined, in your wording.',
    p_notWritten: 'Not written yet', p_openLaw: 'Open', p_loading: 'Loading…',
    p_linesReviewed: 'lines reviewed', p_lines: 'lines', p_confirmed: 'confirmed by the automatic check', p_words: 'words', p_articles: 'articles', p_chaptersWritten: 'chapters written',
    p_sheet: 'One-page sheet', p_map: 'Stakeholder map', p_chaps: 'Chapters', p_doc: 'Public document', p_tag: 'Law in plain language',
    p_notice: 'An explanation next to the law, not the law itself. It does not say how the law applies to one person’s situation. Click an article number to read the original text.',
    p_ok: 'checked, and corrected where needed', p_corrected: 'corrected using the check: a detail the first wording left out was added', p_partly: 'true, but something that matters was left out', p_bad: 'not supported by the article', p_unchecked: 'AI’s own knowledge, not checked',
    p_rewordedDot: 'reworded by a reviewer; the check applied to the AI’s wording',
    p_inShort: 'In short', p_terms: 'Key terms', p_termsS: 'The terms that carry the most weight',
    p_ifThen: 'If … then …', p_ifThenS: 'What triggers a duty', p_who: 'Who owes what to whom', p_whoS: 'The main relations',
    p_purpose: 'What the law is for', p_purposeS: 'The core aim behind the law', p_most: 'The part that matters most', p_mostS: 'Chosen from citations, duties and fines; there is no court data',
    p_exc: 'Main exceptions', p_excS: 'When the general rules do not apply', p_found: 'Where it comes from', p_foundS: 'Constitution, treaties and EU Charter',
    p_if: 'If', p_then: 'then', p_openMap: 'Open the full stakeholder map',
    p_approve: 'Approve', p_decline: 'Decline', p_reword: 'Reword', p_approved: 'approved', p_declined: 'declined', p_rewordedTag: 'reworded',
    p_why: 'Why are you declining this line? (required)', p_whyHint: 'A declined line is left out of the public document.', p_whyErr: 'Explain why you are declining. One sentence is enough.',
    p_yourWording: 'Your wording', p_rewordHint: 'This replaces the AI’s wording in this language only. The AI’s wording stays stored.', p_saveWording: 'Save wording', p_restore: 'Back to the AI’s wording',
    p_check: 'The check: ', p_suggest: 'Suggested wording: ', p_useSuggest: 'Use this wording', p_usedSuggest: 'Wording replaced in both languages',
     p_approvedN: 'Approved {a} lines', p_discard: 'You have typed text that is not saved yet. Leave and discard it?',
    p_mapS: 'Pick a party to see everything that runs from it and to it.', p_owes: 'What this party must or may do', p_owed: 'What others must or may do towards it', p_allRel: 'All relations',
    p_none: 'Nothing here.', p_must: 'must', p_may: 'may', p_must_not: 'must not', p_from: 'from the selected party', p_towards: 'towards the selected party',
    p_chapter: 'Chapter', p_summary: 'Chapter summary', p_byArticle: 'Article by article', p_ofArticle: 'of the article', p_target: 'target', p_upTo: 'limit',
    p_key: 'Key article', p_keyTitle: 'Among the 20% of articles that are cited most, carry the most duties or can lead to a fine.',
    p_forAuth: 'Background or authorities', p_forPublic: 'Duties and rights for people and organisations', p_original: 'Original legal text', p_prevCh: '← Previous chapter', p_nextCh: 'Next chapter →', p_allCh: 'All chapters',
    p_inDoc: 'In the public document, show this chapter as', p_full: 'Full summary', p_gist: 'One sentence', p_hide: 'Leave out', p_inDocHint: 'By default, a chapter with fewer than two duties or rights for people and organisations is shown as one sentence.',
    p_docIntro: 'This is the version to share. It holds the essentials of the law: lines you declined are left out, lines you reworded use your wording, and the review notes are gone.',
    p_print: 'Download as PDF', p_printHint: 'Choose “Save as PDF” in the print window.', p_docWords: 'This document: {a} words. The law: {b} words.',
    p_chapterDocs: 'One chapter in full, with every article explained:', p_wholeDoc: '← The whole document', p_chapterDoc: 'Download this chapter as a document',
    p_draft: 'DRAFT. {a} of {b} lines in this document have been approved by a lawyer. Not for publication yet.', p_allApproved: 'Every line in this document was approved by a lawyer of the Ministry.',
    p_disclaimer: 'This document explains the law in plain language. It is not the law itself, and it cannot tell you how the law applies to your situation. The numbers in brackets are the articles of the law.',
    p_docSub: 'What this law asks, of whom, and what it gives you', p_know: 'Words you need to know', p_do: 'What you must do, and when', p_byChapter: 'The law, chapter by chapter',
    p_chapterSub: 'Chapter {a} of the {b}, explained article by article', p_official: 'Official text: ', p_made: 'Written with AI from the official legal text, checked line by line, and reviewed by lawyers.', p_towardsWho: 'towards'
  };
  var NL = {
    p_title: 'Wetten in gewone taal', p_map_ok: 'goedgekeurd', p_map_no: 'afgewezen', p_map_mixed: 'deels beoordeeld', p_map_none: 'nog niet beoordeeld', p_relCount: '{a} verhoudingen in deze richting', p_bulk: 'Alle regels op deze pagina', p_approveAll: 'Alles goedkeuren ({a})', p_declineAll: 'Alles afwijzen ({a})', p_resetAll: 'Alles terugzetten ({a})', p_declineAllAsk: 'Alle {a} regels op deze pagina afwijzen? Een reden is niet nodig. U kunt ze daarna terugzetten.', p_resetAllAsk: 'Alle {a} regels op deze pagina terugzetten naar niet beoordeeld? Beslissingen en herschrijvingen op deze pagina worden verwijderd.', p_declinedN: '{a} regels afgewezen', p_resetN: '{a} regels teruggezet', p_officialSource: 'Officiële tekst', p_nav: 'Vereenvoudiging', p_cat: 'Elke wet teruggebracht tot wat ze vraagt, van wie en waarom, in woorden die iedereen kan lezen. U keurt elke regel goed voordat die wordt gedeeld.',
    p_lead: 'Voor elke wet schrijft het systeem waar die op neerkomt: één pagina voor de hele wet, een korte samenvatting per hoofdstuk en een paar eenvoudige punten per artikel. U keurt elke regel goed, wijst die af of herschrijft die. Wat u goedkeurt wordt een document dat het ministerie met het publiek kan delen.',
    p_h1: 'Het systeem schrijft', p_h1t: 'op basis van de wettekst en van feiten die het zelf heeft berekend: de begrippen, de plichten, de uitzonderingen.',
    p_h2: 'Een automatische controle', p_h2t: 'leest elke regel naast het artikel en markeert wat een lezer verkeerd kan begrijpen.',
    p_h3: 'U beoordeelt', p_h3t: 'goedkeuren, afwijzen met een reden, of herschrijven. U kunt een beslissing altijd wijzigen.',
    p_h4: 'Delen', p_h4t: 'het publieksdocument bevat alleen wat niet is afgewezen, in uw formulering.',
    p_notWritten: 'Nog niet geschreven', p_openLaw: 'Openen', p_loading: 'Laden…',
    p_linesReviewed: 'regels beoordeeld', p_lines: 'regels', p_confirmed: 'bevestigd door de automatische controle', p_words: 'woorden', p_articles: 'artikelen', p_chaptersWritten: 'hoofdstukken geschreven',
    p_sheet: 'Overzicht op één pagina', p_map: 'Kaart van partijen', p_chaps: 'Hoofdstukken', p_doc: 'Publieksdocument', p_tag: 'Wet in gewone taal',
    p_notice: 'Een uitleg naast de wet, niet de wet zelf. De uitleg zegt niet hoe de wet in iemands situatie uitpakt. Klik op een artikelnummer om de oorspronkelijke tekst te lezen.',
    p_ok: 'gecontroleerd, en waar nodig verbeterd', p_corrected: 'verbeterd met de controle: een detail dat de eerste formulering wegliet is toegevoegd', p_partly: 'klopt, maar iets wat ertoe doet is weggelaten', p_bad: 'niet gedekt door het artikel', p_unchecked: 'eigen kennis van de AI, niet gecontroleerd',
    p_rewordedDot: 'herschreven door een beoordelaar; de controle ging over de formulering van de AI',
    p_inShort: 'In het kort', p_terms: 'Kernbegrippen', p_termsS: 'De begrippen die het zwaarst wegen',
    p_ifThen: 'Als … dan …', p_ifThenS: 'Wat een plicht in gang zet', p_who: 'Wie is wat aan wie verschuldigd', p_whoS: 'De belangrijkste verhoudingen',
    p_purpose: 'Waar de wet voor dient', p_purposeS: 'Het kerndoel achter de wet', p_most: 'Het deel dat het meest telt', p_mostS: 'Gekozen op verwijzingen, plichten en boetes; er zijn geen rechtspraakgegevens',
    p_exc: 'Belangrijkste uitzonderingen', p_excS: 'Wanneer de algemene regels niet gelden', p_found: 'Waar het vandaan komt', p_foundS: 'Grondwet, verdragen en EU-Handvest',
    p_if: 'Als', p_then: 'dan', p_openMap: 'Open de volledige kaart van partijen',
    p_approve: 'Goedkeuren', p_decline: 'Afwijzen', p_reword: 'Herschrijven', p_approved: 'goedgekeurd', p_declined: 'afgewezen', p_rewordedTag: 'herschreven',
    p_why: 'Waarom wijst u deze regel af? (verplicht)', p_whyHint: 'Een afgewezen regel komt niet in het publieksdocument.', p_whyErr: 'Leg uit waarom u afwijst. Eén zin is genoeg.',
    p_yourWording: 'Uw formulering', p_rewordHint: 'Dit vervangt de formulering van de AI alleen in deze taal. De formulering van de AI blijft bewaard.', p_saveWording: 'Formulering opslaan', p_restore: 'Terug naar de formulering van de AI',
    p_check: 'De controle: ', p_suggest: 'Voorgestelde formulering: ', p_useSuggest: 'Gebruik deze formulering', p_usedSuggest: 'Formulering in beide talen vervangen',
     p_approvedN: '{a} regels goedgekeurd', p_discard: 'U heeft tekst getypt die nog niet is opgeslagen. Verlaten en de tekst weggooien?',
    p_mapS: 'Kies een partij om alles te zien wat van en naar die partij loopt.', p_owes: 'Wat deze partij moet of mag doen', p_owed: 'Wat anderen tegenover deze partij moeten of mogen', p_allRel: 'Alle verhoudingen',
    p_none: 'Hier staat niets.', p_must: 'moet', p_may: 'mag', p_must_not: 'mag niet', p_from: 'vanuit de gekozen partij', p_towards: 'naar de gekozen partij',
    p_chapter: 'Hoofdstuk', p_summary: 'Samenvatting van het hoofdstuk', p_byArticle: 'Artikel voor artikel', p_ofArticle: 'van het artikel', p_target: 'doel', p_upTo: 'grens',
    p_key: 'Kernartikel', p_keyTitle: 'Hoort bij de 20% artikelen waarnaar het meest wordt verwezen, die de meeste plichten bevatten of tot een boete kunnen leiden.',
    p_forAuth: 'Achtergrond of autoriteiten', p_forPublic: 'Plichten en rechten voor mensen en organisaties', p_original: 'Oorspronkelijke wettekst', p_prevCh: '← Vorig hoofdstuk', p_nextCh: 'Volgend hoofdstuk →', p_allCh: 'Alle hoofdstukken',
    p_inDoc: 'Toon dit hoofdstuk in het publieksdocument als', p_full: 'Volledige samenvatting', p_gist: 'Eén zin', p_hide: 'Weglaten', p_inDocHint: 'Standaard krijgt een hoofdstuk met minder dan twee plichten of rechten voor mensen en organisaties één zin.',
    p_docIntro: 'Dit is de versie om te delen. Ze bevat de kern van de wet: regels die u heeft afgewezen ontbreken, regels die u heeft herschreven gebruiken uw formulering, en de beoordelingsnotities zijn weg.',
    p_print: 'Downloaden als pdf', p_printHint: 'Kies ‘Opslaan als pdf’ in het afdrukvenster.', p_docWords: 'Dit document: {a} woorden. De wet: {b} woorden.',
    p_chapterDocs: 'Eén hoofdstuk volledig, met uitleg per artikel:', p_wholeDoc: '← Het hele document', p_chapterDoc: 'Download dit hoofdstuk als document',
    p_draft: 'CONCEPT. {a} van de {b} regels in dit document zijn goedgekeurd door een jurist. Nog niet voor publicatie.', p_allApproved: 'Elke regel in dit document is goedgekeurd door een jurist van het ministerie.',
    p_disclaimer: 'Dit document legt de wet uit in gewone taal. Het is niet de wet zelf en het kan u niet vertellen hoe de wet in uw situatie uitpakt. De nummers tussen haakjes zijn de artikelen van de wet.',
    p_docSub: 'Wat deze wet vraagt, van wie, en wat ze u geeft', p_know: 'Woorden die u moet kennen', p_do: 'Wat u moet doen, en wanneer', p_byChapter: 'De wet, hoofdstuk voor hoofdstuk',
    p_chapterSub: 'Hoofdstuk {a} van de {b}, uitgelegd per artikel', p_official: 'Officiële tekst: ', p_made: 'Geschreven met AI op basis van de officiële wettekst, regel voor regel gecontroleerd en beoordeeld door juristen.', p_towardsWho: 'tegenover'
  };
  Object.keys(EN).forEach(function (k) { App.STR.en[k] = EN[k]; });
  Object.keys(NL).forEach(function (k) { App.STR.nl[k] = NL[k]; });

  var INDEX = null, REVIEWS = {}, SETTINGS = {}, LAWS = {}, loading = {};
  // State that must survive a re-render (a language switch, or saving one line).
  var form = null;      // the one open form: { key, mode: 'decline' | 'edit', text, initial, err }
  var party = null;     // party selected on the stakeholder map
  var drawer = null;    // article shown in the original-text drawer

  // ---------- helpers ----------
  function ready() { return !!(INDEX && INDEX.laws.some(function (l) { return l.ready; })); }
  function entry(id) { return INDEX.laws.filter(function (l) { return l.id === id; })[0]; }
  function rv(key) { return REVIEWS[key] || {}; }
  function done(keys) { return keys.filter(function (k) { return rv(k).decision; }).length; }
  function K(law, unit, id) { return law.instrument_id + '::' + unit + '::' + id; }
  function unitKeys(law, names) {
    var out = [];
    names.forEach(function (n) { if (law.units[n]) law.units[n].lines.forEach(function (ln) { out.push(K(law, n, ln.id)); }); });
    return out;
  }
  function nf(n) { return Number(n).toLocaleString(App.lang() === 'nl' ? 'nl-NL' : 'en-GB'); }
  function link(href, text, current) { return h('a', { href: href, 'aria-current': current ? 'page' : null }, text); }
  function plainName(n) { return n.replace(/\s*\([^)]+\)\s*$/, ''); }
  function shortName(n) { var m = /\(([^)]+)\)\s*$/.exec(n); return m ? m[1] : n; }
  function cap(x) { return x.charAt(0).toUpperCase() + x.slice(1); }
  function lowerArticle(x) { return x.replace(/^(The|An|A|De|Het|Een) /, function (m) { return m.toLowerCase(); }); }
  function range(arts) { return arts.length > 1 ? 'art. ' + arts[0] + '–' + arts[arts.length - 1] : 'art. ' + arts[0]; }
  function level(law, ch) { return SETTINGS[law.instrument_id + '::chapter' + ch.number] || (ch.audience === 'public' ? 'full' : 'gist'); }
  function hasUnsaved() { return !!(form && form.text.trim() && form.text.trim() !== (form.initial || '').trim()); }
  function discard() { form = null; drawer = null; }

  function post(url, body) {
    return fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(t('errSave')); return j; }); },
        function () { throw new Error(t('errSave')); });
  }
  function save(key, patch, message) {
    var cur = rv(key), next = { id: key, decision: cur.decision || null, justification: cur.justification || null, edit: cur.edit || null, reviewer: App.reviewer() };
    Object.keys(patch).forEach(function (k) { next[k] = patch[k]; });
    return post('/api/sheet_reviews', next).then(function (res) {
      if (res.entry) REVIEWS[key] = res.entry; else delete REVIEWS[key];
      form = null;
      if (message) App.toast(message, next.decision || '');
      App.rerender();
    }).catch(function (e) { App.toast(e.message, 'decline'); });
  }

  // ---------- one reviewable line ----------
  function refs(arts) {
    return arts.map(function (a) {
      return h('button', { class: 'pref', type: 'button', 'aria-pressed': String(drawer === a),
        onclick: function () { drawer = drawer === a ? null : a; App.rerender(); } }, /^\d/.test(a) ? 'art. ' + a : a);
    });
  }
  function formEl(key, r) {
    var decline = form.mode === 'decline', lang = App.lang();
    var ta = h('textarea', { id: 'pform', name: 'text', autocomplete: 'off', 'aria-describedby': 'pform-err', oninput: function () { form.text = ta.value; } });
    ta.value = form.text;
    var err = h('div', { class: 'err', id: 'pform-err', role: 'alert', text: form.err ? t('p_whyErr') : '' });
    function cancel() { form = null; App.rerender(); }
    return h('form', { class: 'pform', novalidate: true, onsubmit: function (ev) {
      ev.preventDefault();
      var v = ta.value.trim();
      if (decline) {
        if (v.length < App.MIN_JUSTIFICATION) { form.err = true; err.textContent = t('p_whyErr'); ta.setAttribute('aria-invalid', 'true'); ta.focus(); return; }
        save(key, { decision: 'decline', justification: v }, t('savedDecline'));
      } else {
        if (!v) { cancel(); return; }
        var e = {}; if (r.edit) Object.keys(r.edit).forEach(function (k) { e[k] = r.edit[k]; });
        e[lang] = v; save(key, { edit: e });
      }
    } },
      h('label', { for: 'pform', text: decline ? t('p_why') : t('p_yourWording') + ' (' + lang.toUpperCase() + ')' }), ta,
      h('div', { class: 'hint', text: decline ? t('p_whyHint') : t('p_rewordHint') }), err,
      h('div', { class: 'prow' },
        h('button', { class: 'btn primary', type: 'submit' }, decline ? t('p_decline') : t('p_saveWording')),
        !decline && r.edit && r.edit[lang] ? h('button', { class: 'btn', type: 'button', onclick: function () {
          var e = {}; Object.keys(r.edit).forEach(function (k) { if (k !== lang) e[k] = r.edit[k]; });
          save(key, { edit: Object.keys(e).length ? e : null });
        } }, t('p_restore')) : null,
        h('button', { class: 'btn quiet', type: 'button', onclick: cancel }, t('cancel'))));
  }
  function lineRow(law, unit, id, content, opts) {
    var ln = law.units[unit].lines.filter(function (x) { return x.id === id; })[0];
    if (!ln) return null;
    var key = K(law, unit, id), r = rv(key), lang = App.lang(), own = r.edit && r.edit[lang];
    var verdict = own ? 'reworded' : ln.verdict;
    var title = { supported: 'p_ok', corrected: 'p_corrected', partly_supported: 'p_partly', not_supported: 'p_bad', not_checked: 'p_unchecked', reworded: 'p_rewordedDot' }[verdict];
    var txt = h('div', { class: 'ptxt' }, h('span', { class: 'pt' }, own || content), ' ', opts && opts.noRefs ? null : refs(ln.articles));
    if (r.decision) append(txt, h('span', { class: 'pill ' + r.decision, text: t(r.decision === 'accept' ? 'p_approved' : 'p_declined') }));
    if (own) append(txt, h('span', { class: 'pill tag', text: t('p_rewordedTag') }));
    if (verdict === 'not_checked') append(txt, h('span', { class: 'pill', text: t('p_unchecked') }));
    var body = h('div', { class: 'pbody' }, txt);
    if (verdict === 'partly_supported' || verdict === 'not_supported') {
      var sug = ln.suggestion;
      append(body, h('div', { class: 'pnote' },
        h('div', null, h('b', { text: t('p_check') }), L(ln.note)),
        sug ? h('div', { class: 'sug' }, h('b', { text: t('p_suggest') }), L(sug)) : null,
        sug ? h('button', { class: 'btn', type: 'button', onclick: function () { save(key, { edit: { nl: sug.nl, en: sug.en } }, t('p_usedSuggest')); } }, t('p_useSuggest')) : null));
    }
    if (r.decision === 'decline' && r.justification) append(body, h('div', { class: 'pwhy' }, h('b', { text: t('reason') }), r.justification));
    if (form && form.key === key) append(body, formEl(key, r));
    function act(cls, on, label, glyph, fn) {
      return h('button', { class: cls, type: 'button', 'aria-pressed': String(!!on), title: label, 'aria-label': label, onclick: fn }, glyph);
    }
    var acts = h('div', { class: 'pacts' },
      act('a', r.decision === 'accept', t('p_approve'), '✓', function () {
        save(key, { decision: r.decision === 'accept' ? null : 'accept', justification: null }, r.decision === 'accept' ? t('removed') : null);
      }),
      act('d', r.decision === 'decline', t('p_decline'), '✕', function () {
        if (r.decision === 'decline' && !(form && form.key === key)) { save(key, { decision: null, justification: null }, t('removed')); return; }
        form = { key: key, mode: 'decline', text: r.justification || '', initial: r.justification || '' }; App.rerender();
      }),
      act('e', own, t('p_reword'), '✎', function () {
        var start = own || ln[lang];
        form = { key: key, mode: 'edit', text: start, initial: start }; App.rerender();
      }));
    return h('div', { class: 'pl ' + (r.decision || '') },
      h('span', { class: 'pdot ' + verdict, role: 'img', title: t(title), 'aria-label': t(title) }), body, acts);
  }

  // Notice, legend, and one button to approve what the check already confirmed on this page.
  function toolbar(law, names) {
    var keys = [], present = {};    // the legend lists only the colours that appear on this page
    names.forEach(function (n) {
      if (law.units[n]) law.units[n].lines.forEach(function (ln) { present[ln.verdict] = true; keys.push(K(law, n, ln.id)); });
    });
    // One click for every line on this page, mainly for testing and demonstrations. Declining all
    // needs no written reason; resetting removes decisions and rewordings, back to "not reviewed".
    function bulkBtn(cls, label, targets, decision) {
      return h('button', { class: cls, type: 'button', disabled: !targets.length, onclick: function () {
        if (decision !== 'accept' && !window.confirm(t(decision === 'decline' ? 'p_declineAllAsk' : 'p_resetAllAsk', targets.length))) return;
        post('/api/sheet_reviews', { items: targets.map(function (k) {
          return decision === null ? { id: k, decision: null, edit: null }
            : { id: k, decision: decision, bulk: decision === 'decline', justification: null, edit: rv(k).edit || null };
        }) }).then(function (res) {
          Object.keys(res.entries).forEach(function (k) { if (res.entries[k]) REVIEWS[k] = res.entries[k]; else delete REVIEWS[k]; });
          App.toast(t(decision === 'accept' ? 'p_approvedN' : decision === 'decline' ? 'p_declinedN' : 'p_resetN', targets.length), decision || '');
          form = null; App.rerender();
        }).catch(function (e) { App.toast(e.message, 'decline'); });
      } }, t(label, targets.length));
    }
    function dot(cls, label) { return h('span', null, h('i', { class: 'pdot ' + cls }), t(label)); }
    return [
      h('div', { class: 'pnotice', role: 'note', text: t('p_notice') }),
      h('div', { class: 'pbar' },
        h('div', { class: 'plegend' }, [['supported', 'p_ok'], ['partly_supported', 'p_partly'], ['not_supported', 'p_bad'], ['not_checked', 'p_unchecked']]
          .filter(function (x) { return x[0] === 'supported' || present[x[0]]; }).map(function (x) { return dot(x[0], x[1]); })),
        h('div', { class: 'pbulk', role: 'group', 'aria-label': t('p_bulk') },
          bulkBtn('btn', 'p_approveAll', keys.filter(function (k) { return rv(k).decision !== 'accept'; }), 'accept'),
          bulkBtn('btn', 'p_declineAll', keys.filter(function (k) { return rv(k).decision !== 'decline'; }), 'decline'),
          bulkBtn('btn quiet', 'p_resetAll', keys.filter(function (k) { return REVIEWS[k]; }), null)))
    ];
  }
  function card(cls, title, step, rows) {
    return h('section', { class: 'card pcard ' + cls }, h('h2', { text: t(title) }), step ? h('div', { class: 'step', text: t(step) }) : null, rows);
  }

  // ---------- sidebar and overview hooks ----------
  function navLinks(active, lawId) {
    var tot = 0, n = 0;
    INDEX.laws.forEach(function (l) { if (l.ready) { tot += l.keys.length; n += done(l.keys); } });
    var out = [h('a', { class: 'nav-link', href: '#/s', 'aria-current': active && !lawId ? 'page' : null },
      t('p_nav'), h('span', { class: 'n', text: n + '/' + tot }))];
    // Every law in the collection; the ones not written yet are listed but not clickable.
    if (active) out.push(h('div', { class: 'nav-sub' }, INDEX.laws.map(function (l) {
      if (!l.ready) return h('span', { class: 'nav-soon' }, L(l.short), h('span', { class: 'n', text: t('soon') }));
      return h('a', { href: '#/s/' + l.id, 'aria-current': l.id === lawId ? 'true' : null }, L(l.short), h('span', { class: 'n', text: String(l.keys.length) }));
    })));
    return out;
  }
  function overviewCard(still) {
    var laws = INDEX.laws.filter(function (l) { return l.ready; }), tot = 0, n = 0;
    laws.forEach(function (l) { tot += l.keys.length; n += done(l.keys); });
    return h('div', { class: 'card cat live' },
      h('div', { class: 'cat-top' }, App.ringBox(tot ? n / tot : 0, 72, Math.round(100 * (tot ? n / tot : 0)) + '%', true, still),
        h('div', null, h('h3', { style: 'font-size:1.2rem', text: t('p_title') }), h('p', { text: t('p_cat') }))),
      h('div', { class: 'bars' + (still ? ' still' : '') }, laws.map(function (l) {
        var d = done(l.keys);
        return h('a', { class: 'barrow', href: '#/s/' + l.id }, h('span', { text: L(l.short) }),
          h('span', { class: 'tr' }, h('i', { style: 'width:' + Math.max(4, Math.round(100 * d / l.keys.length)) + '%' })),
          h('span', { class: 'v', style: 'grid-column:auto;white-space:nowrap', text: d + '/' + l.keys.length }));
      })),
      h('a', { class: 'btn primary', href: '#/s' }, t('open', t('p_title').toLowerCase())));
  }

  // ---------- the laws ----------
  function listScreen() {
    document.title = t('p_title') + ' – ' + t('title');
    App.renderSide('simplification');
    return [
      h('nav', { class: 'crumbs', 'aria-label': t('breadcrumb') }, link('#/', t('overview')), ' / ' + t('p_title')),
      h('div', { class: 'pagehead' }, h('h1', { text: t('p_title') }), h('p', { style: 'max-width:74ch', text: t('p_lead') })),
      h('ol', { class: 'psteps' }, [1, 2, 3, 4].map(function (i) {
        return h('li', { class: 'card' }, h('span', { class: 'n', text: String(i) }), h('div', null, h('b', { text: t('p_h' + i) }), h('span', { text: ' ' + t('p_h' + i + 't') })));
      })),
      h('div', { class: 'cats', style: 'margin-top:24px' }, INDEX.laws.map(function (l) {
        if (!l.ready) return h('div', { class: 'card cat off' }, h('div', null, h('h3', { text: L(l.name) })), h('div', null, h('span', { class: 'pill', text: t('p_notWritten') })));
        var d = done(l.keys), tot = l.keys.length;
        return h('div', { class: 'card cat live' },
          h('div', { class: 'cat-top' }, App.ringBox(d / tot, 72, Math.round(100 * d / tot) + '%', true, true),
            h('div', null, h('h3', { style: 'font-size:1.2rem', text: L(l.name) }),
              h('p', { text: nf(l.n_articles) + ' ' + t('p_articles') + ' · ' + nf(l.n_words) + ' ' + t('p_words') + ' · ' + l.n_chapters_written + '/' + l.n_chapters + ' ' + t('p_chaptersWritten') }))),
          h('p', { class: 'muted', style: 'margin:0;font-size:.94rem', text: d + ' ' + t('of') + ' ' + tot + ' ' + t('p_linesReviewed') + ' · ' + l.n_confirmed + ' ' + t('p_confirmed') }),
          h('a', { class: 'btn primary', href: '#/s/' + l.id }, t('p_openLaw')));
      }))
    ];
  }

  function head(law, view, chapterNo) {
    var e = entry(law.id), d = done(e.keys), tot = e.keys.length, base = '#/s/' + law.id;
    var crumbs = [link('#/', t('overview')), ' / ', link('#/s', t('p_title')), ' / ', view === 'sheet' ? L(law.short) : link(base, L(law.short))];
    if (view === 'ch') crumbs.push(' / ', link(base + '/chapters', t('p_chaps')), ' / ' + t('p_chapter') + ' ' + chapterNo);
    function tab(v, href, label) { return h('a', { href: href, 'aria-current': v.indexOf(view) > -1 ? 'page' : null }, label); }
    return [
      h('nav', { class: 'crumbs', 'aria-label': t('breadcrumb') }, crumbs),
      h('header', { class: 'qhero' },
        h('span', { class: 'pill tag', text: t('p_tag') }),
        h('h1', { text: L(law.name) }),
        h('div', { class: 'qmeta' },
          h('span', { text: nf(law.n_articles) + ' ' + t('p_articles') + ' · ' + nf(law.n_words) + ' ' + t('p_words') }),
          h('span', { text: d + ' ' + t('of') + ' ' + tot + ' ' + t('p_linesReviewed') }),
          h('span', { class: 'posbar', 'aria-hidden': 'true' }, h('i', { style: 'width:' + (100 * d / tot) + '%' })),
          h('span', { text: e.n_confirmed + ' ' + t('p_confirmed') }))),
      h('nav', { class: 'ptabs', 'aria-label': L(law.short) },
        tab(['sheet'], base, t('p_sheet')), law.units.map ? tab(['map'], base + '/map', t('p_map')) : null,
        tab(['chapters', 'ch'], base + '/chapters', t('p_chaps')), tab(['doc'], base + '/doc', t('p_doc')))
    ];
  }

  // ---------- one-page sheet ----------
  function sheetScreen(law) {
    var c = law.units.law.content, lang = App.lang();
    function row(id, content) { return lineRow(law, 'law', id, content); }
    function simple(key, list) { return list.map(function (l, i) { return row(key + i, L(l)); }); }
    return [toolbar(law, ['law']),
      h('div', { class: 'pgrid' },
        card('wide lead', 'p_inShort', null, simple('in_short', c.in_short)),
        card('', 'p_terms', 'p_termsS', c.core_concepts.map(function (x, i) {
          return row('core_concepts' + i, [h('b', { text: lang === 'nl' ? x.term : x.term_en }), lang === 'en' ? h('i', { class: 'muted', lang: 'nl', text: ' (' + x.term + ')' }) : null, h('br'), L(x.plain)]);
        })),
        card('', 'p_who', 'p_whoS', [c.stakeholders.map(function (x, i) {
          return row('stakeholders' + i, [h('b', { text: L(x.who) }), h('span', { class: 'ar', text: ' → ' }), h('b', { text: L(x.to_whom) }), h('br'), L(x.owes)]);
        }), law.units.map ? h('p', { style: 'margin:10px 0 6px' }, link('#/s/' + law.id + '/map', t('p_openMap') + ' →')) : null]),
        card('wide', 'p_ifThen', 'p_ifThenS', c.if_then.map(function (x, i) {
          return row('if_then' + i, h('span', { class: 'pif' }, h('span', { class: 'if', text: t('p_if') + ' ' + L(x.if_) }), h('span', { text: t('p_then') + ' ' + L(x.then) })));
        })),
        card('', 'p_most', 'p_mostS', simple('most_consequential', c.most_consequential)),
        card('', 'p_exc', 'p_excS', simple('exceptions', c.exceptions)),
        card('', 'p_purpose', 'p_purposeS', row('purpose', L(c.purpose))),
        card('', 'p_found', 'p_foundS', c.foundations.map(function (f, i) { return row('foundations' + i, [h('b', { text: L(f.source) }), h('br'), L(f.plain)]); })))];
  }

  // ---------- stakeholder map ----------
  // Parties on an ellipse, one curved arrow per direction, all the same width, with the number of
  // relations it stands for. In the workspace each arrow is coloured by how far its relations are
  // reviewed; in a document the arrows are plain and declined relations are left out (`keep`).
  function mapBlock(law, interactive, keep) {
    var m = law.units.map.content, P = m.parties, n = P.length, pos = {}, pairs = {};
    P.forEach(function (p, i) { var a = -Math.PI / 2 + 2 * Math.PI * i / n; pos[p.id] = [500 + 380 * Math.cos(a), 280 + 215 * Math.sin(a)]; });
    m.relations.forEach(function (r, i) {
      if (keep && !keep(i)) return;
      var k = r.from_id + '>' + r.to_id, d = rv(K(law, 'map', 'rel' + i)).decision;
      var e = pairs[k] = pairs[k] || { n: 0, acc: 0, dec: 0 };
      e.n++; if (d === 'accept') e.acc++; if (d === 'decline') e.dec++;
    });
    function status(e) {
      if (!interactive) return 'none';
      return e.dec === e.n ? 'no' : e.acc === e.n ? 'ok' : e.acc + e.dec ? 'mixed' : 'none';
    }
    var sel = interactive ? party : null;
    function marker(st) {
      return s('marker', { id: 'pmk-' + st, viewBox: '0 0 10 10', refX: 8, refY: 5, markerWidth: 13, markerHeight: 13, markerUnits: 'userSpaceOnUse', orient: 'auto-start-reverse' },
        s('path', { d: 'M0 0L10 5L0 10z', 'class': 'mk st-' + st }));
    }
    var svg = s('svg', { viewBox: '0 0 1000 560', preserveAspectRatio: 'none', 'aria-hidden': 'true' },
      s('defs', {}, marker('none'), marker('ok'), marker('no'), marker('mixed')));
    var weights = [];
    Object.keys(pairs).forEach(function (k) {
      var a = k.split('>')[0], b = k.split('>')[1];
      if (!pos[a] || !pos[b]) return;
      var x1 = pos[a][0], y1 = pos[a][1], x2 = pos[b][0], y2 = pos[b][1], dx = x2 - x1, dy = y2 - y1, len = Math.sqrt(dx * dx + dy * dy) || 1;
      var ux = dx / len, uy = dy / len, edge = 1 / Math.sqrt(Math.pow(ux / 92, 2) + Math.pow(uy / 52, 2));   // stop at the box, so the arrowhead shows
      x1 += ux * edge; y1 += uy * edge; x2 -= ux * edge; y2 -= uy * edge;
      var cx = (x1 + x2) / 2 - uy * 26, cy = (y1 + y2) / 2 + ux * 26;
      var st = status(pairs[k]), focus = !sel ? '' : a === sel || b === sel ? ' sel' : ' dim';
      svg.appendChild(s('path', { 'class': 'e st-' + st + focus, d: 'M' + x1 + ' ' + y1 + ' Q' + cx + ' ' + cy + ' ' + x2 + ' ' + y2,
        'stroke-width': 2, 'marker-end': 'url(#pmk-' + st + ')' }));
      var mx = 0.25 * x1 + 0.5 * cx + 0.25 * x2, my = 0.25 * y1 + 0.5 * cy + 0.25 * y2;   // middle of the curve
      weights.push(h('span', { class: 'pw st-' + st + focus, style: 'left:' + (mx / 10) + '%;top:' + (my / 5.6) + '%',
        title: t('p_relCount', pairs[k].n), text: String(pairs[k].n) }));
    });
    function touches(id) {
      return !sel || id === sel || m.relations.some(function (r) { return (r.from_id === sel && r.to_id === id) || (r.to_id === sel && r.from_id === id); });
    }
    return h('div', { class: 'pmap' }, svg, weights, P.map(function (p) {
      var label = plainName(L(p.name));
      var style = 'left:' + (pos[p.id][0] / 10) + '%;top:' + (pos[p.id][1] / 5.6) + '%';
      if (!interactive) return h('div', { class: 'pnode', style: style }, label);
      return h('button', { class: 'pnode' + (touches(p.id) ? '' : ' dim'), type: 'button', style: style, 'aria-pressed': String(p.id === sel),
        onclick: function () { party = party === p.id ? null : p.id; App.rerender(); } }, label);
    }));
  }
  function mapScreen(law) {
    var m = law.units.map.content;
    function name(id) { var p = m.parties.filter(function (x) { return x.id === id; })[0]; return p ? cap(shortName(L(p.name))) : id; }
    function rel(pair) {
      var r = pair[0];
      return lineRow(law, 'map', 'rel' + pair[1], [h('span', { class: 'kind ' + r.kind, text: t('p_' + r.kind) }), h('b', { text: name(r.from_id) }),
        h('span', { class: 'ar', text: ' → ' }), h('b', { text: name(r.to_id) }), h('br'), L(r.what)]);
    }
    var idx = m.relations.map(function (r, i) { return [r, i]; });
    if (party && !m.parties.some(function (p) { return p.id === party; })) party = null;
    var out = idx.filter(function (p) { return !party || p[0].from_id === party; });
    var inn = party ? idx.filter(function (p) { return p[0].to_id === party; }) : [];
    var sel = m.parties.filter(function (p) { return p.id === party; })[0];
    function list(title, items) { return h('section', { class: 'card pcard' }, h('h2', { text: t(title) }), items.length ? items.map(rel) : h('p', { class: 'muted', text: t('p_none') })); }
    return [toolbar(law, ['map']),
      h('section', { class: 'card pcard' }, h('h2', { text: t('p_who') }), h('div', { class: 'step', text: t('p_mapS') }), mapBlock(law, true),
        h('div', { class: 'pmaplegend' }, ['ok', 'no', 'mixed', 'none'].map(function (st) { return h('span', null, h('i', { class: 'st-' + st }), t('p_map_' + st)); }))),
      sel ? h('div', { class: 'covered', style: 'margin:16px 0' }, h('b', { text: L(sel.name) }), ' — ' + L(sel.role)) : null,
      h('div', { class: party ? 'pcols' : '', style: 'margin-top:16px' }, list(party ? 'p_owes' : 'p_allRel', out), party ? list('p_owed', inn) : null)];
  }

  // ---------- chapters ----------
  function chapterTitle(law, ch) { var u = law.units['chapter' + ch.number]; return u ? L(u.content.title) : ch.legal_title; }
  // Every chapter one click away, kept in view while scrolling. Used on the chapter pages and,
  // linking to the chapter documents, on the public document page.
  function chapterBar(law, current, hrefBase, field) {
    var bar = h('nav', { class: 'pchbar noprint', 'aria-label': t('p_chaps') }, law.chapters.map(function (c) {
      var label = [h('span', { class: 'no', text: String(c.number) }), h('span', { class: 'nm', text: chapterTitle(law, c) })];
      return c[field]
        ? h('a', { href: hrefBase + c.number, 'aria-current': c.number === current ? 'page' : null, title: chapterTitle(law, c) }, label)
        : h('span', { class: 'off', title: t('p_notWritten') }, label);
    }));
    setTimeout(function () {           // bring the current chapter into view inside the bar
      var cur = bar.querySelector('[aria-current=page]');
      if (cur) bar.scrollLeft = cur.offsetLeft - bar.offsetLeft - (bar.clientWidth - cur.offsetWidth) / 2;
    }, 0);
    return bar;
  }
  function chaptersScreen(law) {
    return [h('ul', { class: 'pchlist' }, law.chapters.map(function (ch) {
      var names = ['chapter' + ch.number, 'articles' + ch.number], keys = unitKeys(law, names), d = done(keys);
      var keyArts = ch.articles.filter(function (a) { return law.articles[a] && law.articles[a].key; }).length;
      var inner = [h('span', { class: 'num', text: String(ch.number) }),
        h('span', { class: 't', text: chapterTitle(law, ch) }),
        h('span', { class: 'm' }, h('span', { lang: 'nl', text: ch.legal_title }), h('span', { text: range(ch.articles) + ' · ' + nf(ch.n_words) + ' ' + t('p_words') }),
          h('span', { class: 'pill ' + (ch.audience === 'public' ? 'tag' : ''), text: t(ch.audience === 'public' ? 'p_forPublic' : 'p_forAuth') }),
          keyArts ? h('span', { class: 'pill warn', title: t('p_keyTitle'), text: keyArts + ' × ' + t('p_key').toLowerCase() }) : null),
        h('span', { class: 'st' }, ch.has_summary ? d + '/' + keys.length + ' ' + t('p_lines') : h('span', { class: 'pill', text: t('p_notWritten') }),
          ch.has_summary ? h('span', { class: 'arrow', 'aria-hidden': 'true', text: '→' }) : null)];
      return h('li', { class: 'pch' + (ch.has_summary ? '' : ' off') },
        ch.has_summary ? h('a', { class: 'card', href: '#/s/' + law.id + '/ch/' + ch.number }, inner) : h('div', { class: 'card' }, inner));
    }))];
  }
  function chapterScreen(law, n) {
    var ch = law.chapters[n - 1], u = law.units['chapter' + n], cards = law.units['articles' + n], base = '#/s/' + law.id;
    var lv = level(law, ch), lang = App.lang();
    var prev = law.chapters[n - 2], next = law.chapters[n];
    function setLevel(v) {
      var id = law.instrument_id + '::chapter' + n;
      post('/api/sheet_settings', { id: id, value: v }).then(function () { SETTINGS[id] = v; App.rerender(); }).catch(function (e) { App.toast(e.message, 'decline'); });
    }
    var switcher = chapterBar(law, n, base + '/ch/', 'has_summary');
    var out = [switcher, toolbar(law, ['chapter' + n, 'articles' + n]),
      h('section', { class: 'card pcard' },
        h('div', { class: 'parthead' }, h('h3', { style: 'font-size:1.3rem', text: L(u.content.title) }),
          h('span', { class: 'plen', text: u.n_words[lang] + ' ' + t('p_words') + ' · ' + t('p_target') + ' ' + u.target_words[0] + '–' + u.target_words[1] })),
        h('div', { class: 'step' }, h('span', { lang: 'nl', text: ch.legal_title }), ' · ' + range(ch.articles)),
        lineRow(law, 'chapter' + n, 'about', L(u.content.about)),
        u.content.lines.map(function (l, i) { return lineRow(law, 'chapter' + n, 'line' + i, L(l)); }),
        h('div', { class: 'pindoc' }, h('span', { text: t('p_inDoc') }),
          h('div', { class: 'segctl', role: 'group', 'aria-label': t('p_inDoc') }, ['full', 'gist', 'hide'].map(function (v) {
            return h('button', { type: 'button', 'aria-pressed': String(lv === v), onclick: function () { setLevel(v); } }, t('p_' + v));
          })),
          h('span', { class: 'hint', text: t('p_inDocHint') })))];
    if (cards) {
      out.push(h('div', { class: 'psub' }, h('h2', { text: t('p_byArticle') }), link(base + '/doc/' + n, t('p_chapterDoc') + ' →')));
      cards.content.cards.forEach(function (c) {
        var src = law.articles[c.article] || {}, w = c.n_words[lang], pct = c.source_words ? Math.round(100 * w / c.source_words) : 0;
        out.push(h('section', { class: 'card pcard' },
          h('div', { class: 'parthead' },
            h('h3', null, L(c.title), ' ', refs([c.article]), src.key ? h('span', { class: 'pill warn', title: t('p_keyTitle'), text: t('p_key') }) : null),
            h('span', { class: 'plen' }, w + ' / ' + c.source_words + ' ' + t('p_words') + ' · ' + pct + '% ' + t('p_ofArticle'),
              h('span', { class: 'bar' + (w > c.target_words[1] * 1.1 ? ' over' : ''), title: t('p_upTo') + ' ' + c.target_words[1] + ' ' + t('p_words') }, h('i', { style: 'width:' + Math.min(pct, 100) + '%' })))),
          c.points.map(function (p, i) { return lineRow(law, 'articles' + n, 'art' + c.article + '_' + i, L(p), { noRefs: true }); })));
      });
    }
    out.push(h('nav', { class: 'pager', 'aria-label': t('p_chaps') },
      prev && prev.has_summary ? h('a', { class: 'btn', href: base + '/ch/' + prev.number }, t('p_prevCh')) : h('span'),
      h('a', { class: 'btn quiet', href: base + '/chapters' }, t('p_allCh')),
      next && next.has_summary ? h('a', { class: 'btn', href: base + '/ch/' + next.number }, t('p_nextCh')) : h('span')));
    return out;
  }

  // ---------- the public document ----------
  // What leaves the Ministry: no declined lines, the reviewer's wording, no notes. The whole law fits a
  // few pages because a chapter with hardly any duties or rights for people and organisations is one sentence, and the article cards
  // live in a separate document per chapter.
  function docScreen(law, n) {
    var lang = App.lang(), included = [], base = '#/s/' + law.id;
    function keep(unit, id) { return rv(K(law, unit, id)).decision !== 'decline'; }
    function own(unit, id) { var e = rv(K(law, unit, id)).edit; return e && e[lang]; }
    function r(arts) { return arts && arts.length ? h('span', { class: 'r', text: ' (' + arts.map(function (a) { return /^\d/.test(a) ? 'art. ' + a : a; }).join(', ') + ')' }) : null; }
    function item(tag, unit, id, content, arts) {
      if (!keep(unit, id)) return null;
      included.push(K(law, unit, id));
      return h(tag, null, own(unit, id) || content, r(arts));
    }
    function li(unit, id, content, arts) { return item('li', unit, id, content, arts); }
    function list(cls, items) { items = items.filter(Boolean); return items.length ? h('ul', { class: cls }, items) : null; }
    function sec(title, body, cls) { return body ? h('section', { class: cls || '' }, h('h2', { text: t(title) }), body) : null; }

    var doc = h('article', { class: 'doc', lang: lang });
    var status = h('div', { class: 'box' });
    var c = law.units.law.content;

    if (n) {            // one chapter, article by article
      var ch = law.chapters[n - 1], u = law.units['chapter' + n], cards = law.units['articles' + n];
      document.title = L(u.content.title) + ' – ' + L(law.short);
      append(doc, [
        h('header', { class: 'dh' }, h('div', { class: 'eyebrow', text: L(law.name) + ' · ' + t('p_tag').toLowerCase() }), h('h1', { text: L(u.content.title) }),
          h('p', { class: 'sub', text: t('p_chapterSub', n, L(law.short)) + ' (' + range(ch.articles) + ')' })),
        status, h('div', { class: 'box', text: t('p_disclaimer') }),
        list('lead', [li('chapter' + n, 'about', L(u.content.about), u.content.about.articles)].concat(u.content.lines.map(function (l, i) { return li('chapter' + n, 'line' + i, L(l), l.articles); }))),
        cards ? h('div', { class: 'cols' }, cards.content.cards.map(function (cd) {
          var pts = list('', cd.points.map(function (p, i) { return li('articles' + n, 'art' + cd.article + '_' + i, L(p)); }));
          return pts ? h('div', { class: 'chap' }, h('h3', null, L(cd.title), h('span', { class: 'r', text: ' (art. ' + cd.article + ')' })), pts) : null;
        })) : null
      ]);
    } else {
      document.title = L(law.short) + ' – ' + t('p_tag').toLowerCase();
      var m = law.units.map && law.units.map.content;
      var terms = c.core_concepts.map(function (x, i) {
        if (!keep('law', 'core_concepts' + i)) return null;
        included.push(K(law, 'law', 'core_concepts' + i));
        return own('law', 'core_concepts' + i) ? h('div', null, h('dd', { text: own('law', 'core_concepts' + i) })) :
          h('div', null, h('dt', null, lang === 'nl' ? x.term : x.term_en, lang === 'en' ? h('span', { class: 'r', lang: 'nl', text: ' ' + x.term }) : null), h('dd', null, L(x.plain), r(x.articles)));
      }).filter(Boolean);
      var rules = c.if_then.map(function (x, i) {
        if (!keep('law', 'if_then' + i)) return null;
        included.push(K(law, 'law', 'if_then' + i));
        return own('law', 'if_then' + i) ? h('tr', null, h('td', { colspan: '2', style: 'background:none;font-weight:400', text: own('law', 'if_then' + i) })) :
          h('tr', null, h('td', { text: cap(L(x.if_)) }), h('td', null, cap(L(x.then)), r(x.articles)));
      }).filter(Boolean);
      var parties = m ? m.parties.map(function (p) {
        var items = m.relations.map(function (rel, i) { return [rel, i]; }).filter(function (x) { return x[0].from_id === p.id; }).map(function (x) {
          var to = m.parties.filter(function (q) { return q.id === x[0].to_id; })[0];
          return li('map', 'rel' + x[1], [h('span', { class: 'to', text: '→ ' + (to ? lowerArticle(shortName(L(to.name))) : x[0].to_id) + ': ' }), L(x[0].what)], x[0].articles);
        });
        var ul = list('', items);
        return ul ? h('div', { class: 'party' }, h('h3', { text: cap(plainName(L(p.name))) }), h('p', { class: 'role', text: L(p.role) }), ul) : null;
      }).filter(Boolean) : [];
      var chapters = law.chapters.map(function (ch) {
        var u = law.units['chapter' + ch.number], lv = level(law, ch);
        if (!u || lv === 'hide') return null;
        var id = 'chapter' + ch.number;
        var head3 = h('h3', null, ch.number + '. ' + L(u.content.title), h('span', { class: 'r', text: ' (' + range(ch.articles) + ')' }));
        var body = lv === 'gist' ? item('p', id, 'about', L(u.content.about)) :
          list('', [li(id, 'about', L(u.content.about))].concat(u.content.lines.map(function (l, i) { return li(id, 'line' + i, L(l), l.articles); })));
        return body ? h('div', { class: 'chap' + (lv === 'full' ? ' long' : '') }, head3, body) : null;
      }).filter(Boolean);
      var purpose = item('p', 'law', 'purpose', L(c.purpose), c.purpose.articles);
      append(doc, [
        h('header', { class: 'dh' }, h('div', { class: 'eyebrow', text: t('p_tag') }), h('h1', { text: L(law.name) }), h('p', { class: 'sub', text: t('p_docSub') })),
        status, h('div', { class: 'box', text: t('p_disclaimer') }),
        sec('p_inShort', list('lead', c.in_short.map(function (l, i) { return li('law', 'in_short' + i, L(l), l.articles); }))),
        purpose ? h('section', null, h('h2', { text: t('p_purpose') }), purpose) : null,
        h('div', { class: 'two' },
          terms.length ? h('section', null, h('h2', { text: t('p_know') }), h('dl', { class: 'terms' }, terms)) : null,
          sec('p_most', list('', c.most_consequential.map(function (l, i) { return li('law', 'most_consequential' + i, L(l), l.articles); })))),
        rules.length ? h('section', null, h('h2', { text: t('p_do') }), h('table', { class: 'it' },
          h('thead', null, h('tr', null, h('th', { scope: 'col', text: t('p_if') + ' …' }), h('th', { scope: 'col', text: '… ' + t('p_then') }))), h('tbody', null, rules))) : null,
        h('div', { class: 'two' },
          sec('p_exc', list('', c.exceptions.map(function (l, i) { return li('law', 'exceptions' + i, L(l), l.articles); }))),
          sec('p_found', list('', c.foundations.map(function (f, i) { return li('law', 'foundations' + i, [h('b', { text: L(f.source) + ': ' }), L(f.plain)], (law.units.law.lines.filter(function (x) { return x.id === 'foundations' + i; })[0] || {}).articles); })))),
        parties.length ? h('section', null, h('h2', { text: t('p_who') }), mapBlock(law, false, function (i) { return keep('map', 'rel' + i); }), h('div', { class: 'cols' }, parties)) : null,
        chapters.length ? h('section', null, h('h2', { text: t('p_byChapter') }), h('div', { class: 'cols' }, chapters)) : null
      ]);
    }
    append(doc, h('footer', { class: 'foot' }, h('p', null, t('p_official'), h('span', { class: 'url', text: law.source_url })), h('p', { text: t('p_made') })));

    var approved = included.filter(function (k) { return rv(k).decision === 'accept'; }).length;
    if (included.length && approved === included.length) { status.className = 'box ok'; status.textContent = t('p_allApproved'); }
    else { status.className = 'box draft'; status.textContent = t('p_draft', approved, included.length); }
    var words = doc.textContent.trim().split(/\s+/).length;
    return [
      h('div', { class: 'pdocbar noprint' },
        h('p', { text: t('p_docIntro') }),
        h('div', { class: 'prow', style: 'align-items:center' },
          h('button', { class: 'btn primary', type: 'button', onclick: function () { window.print(); } }, t('p_print')),
          h('span', { class: 'hint', text: t('p_printHint') + ' ' + t('p_docWords', nf(words), nf(law.n_words)) })),
        h('p', { class: 'pchdocs' }, n ? link(base + '/doc', t('p_wholeDoc')) : t('p_chapterDocs'))),
      chapterBar(law, n, base + '/doc/', 'has_cards'),
      doc
    ];
  }

  function drawerEl(law) {
    var a = law.articles[drawer];
    return h('aside', { class: 'pdrawer', 'aria-label': t('p_original') },
      h('button', { class: 'iconbtn x', type: 'button', 'aria-label': t('close'), onclick: function () { drawer = null; App.rerender(); } }, '×'),
      h('div', { class: 'lbl', text: t(a.official_source ? 'p_officialSource' : 'p_original') }), h('h2', { lang: 'nl', text: a.heading }),
      a.key ? h('p', null, h('span', { class: 'pill warn', title: t('p_keyTitle'), text: t('p_key') })) : null,
      h('div', { class: 'tx', lang: 'nl', tabindex: '0', text: a.text }));
  }

  // ---------- route ----------
  function load(id) {
    if (loading[id]) return;
    loading[id] = true;
    fetch('data/simplified_' + id + '.json').then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
      .then(function (d) { LAWS[id] = d; App.rerender(); }, function () { loading[id] = false; App.toast(t('loadErr'), 'decline'); });
  }
  function screen(parts) {
    App.setPage({ kind: 'plain' });
    var e = parts[0] && entry(parts[0]);
    if (!e || !e.ready) return listScreen();
    App.renderSide('simplification', e.id);
    var law = LAWS[e.id];
    if (!law) { load(e.id); return [h('p', { class: 'muted', style: 'padding:40px 0', text: t('p_loading') })]; }
    var view = parts[1] || 'sheet', n = parseInt(parts[2], 10);
    if (!law.units['chapter' + n]) n = null;
    document.title = L(law.short) + ' – ' + t('p_title');
    var body;
    if (view === 'map' && law.units.map) body = mapScreen(law);
    else if (view === 'chapters') body = chaptersScreen(law);
    else if (view === 'ch' && n) body = chapterScreen(law, n);
    else if (view === 'doc') body = docScreen(law, n);
    else { view = 'sheet'; body = sheetScreen(law); }
    var out = head(law, view, n).concat(body);
    if (drawer && law.articles[drawer] && view !== 'doc') out.push(drawerEl(law));
    if (form) setTimeout(function () { var f = document.getElementById('pform'); if (f && document.activeElement !== f) { f.focus(); f.setSelectionRange(f.value.length, f.value.length); } }, 0);
    return out;
  }
  document.addEventListener('keydown', function (ev) {
    if (ev.key !== 'Escape' || !(drawer || form) || hasUnsaved()) return;
    drawer = null; form = null; App.rerender();
  });

  App.plain = {
    init: function (index, reviews, settings) { INDEX = index; REVIEWS = reviews || {}; SETTINGS = settings || {}; },
    ready: ready, navLinks: navLinks, overviewCard: overviewCard, screen: screen, hasUnsaved: hasUnsaved, discard: discard,
    progress: function () {
      var out = { done: 0, acc: 0, dec: 0, total: 0 };
      INDEX.laws.forEach(function (l) {
        if (!l.ready) return;
        out.total += l.keys.length;
        l.keys.forEach(function (k) { var d = rv(k).decision; if (d) out.done++; if (d === 'accept') out.acc++; if (d === 'decline') out.dec++; });
      });
      return out;
    }
  };
})();
