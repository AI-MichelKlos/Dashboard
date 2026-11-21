(function () {
  'use strict';
  const ROOT = 'https://ai-michelklos.github.io/Dashboard/';
  const ALLOWED_MONTHS = new Set(['12','36','60']);

  function selectedMonths() {
    const value = document.querySelector('#dak-dashboard [data-months].active')?.dataset.months || '60';
    return ALLOWED_MONTHS.has(value) ? value : '60';
  }
  function chartLink(id) {
    const url = new URL('share/' + encodeURIComponent(id) + '/', ROOT);
    url.searchParams.set('months', selectedMonths());
    return url.href;
  }
  function shareOnLinkedIn(url) {
    window.open('https://www.linkedin.com/sharing/share-offsite/?url=' + encodeURIComponent(url),
      '_blank', 'noopener,noreferrer');
  }
  async function copyLink(button, url) {
    try {
      await navigator.clipboard.writeText(url);
      const saved = button.textContent;
      button.textContent = 'Link kopieret';
      setTimeout(() => { button.textContent = saved; }, 2200);
    } catch (_) { window.prompt('Kopiér linket til grafen:', url); }
  }

  function addButtons(card) {
    const canvas = card.querySelector('canvas[id]');
    const chartBox = card.querySelector('.chart-wrap');
    if (!canvas || !chartBox || card.querySelector('.mk-linkedin-buttons')) return;
    const label = (card.querySelector('h3')?.textContent || 'graf').trim();
    const actions = document.createElement('div');
    actions.className = 'mk-linkedin-buttons';
    const linkedin = document.createElement('button');
    linkedin.type = 'button';
    linkedin.className = 'mk-share-linkedin';
    linkedin.textContent = 'Del på LinkedIn';
    linkedin.setAttribute('aria-label', 'Del ' + label + ' på LinkedIn');
    linkedin.addEventListener('click', () => shareOnLinkedIn(chartLink(canvas.id)));
    const copy = document.createElement('button');
    copy.type = 'button';
    copy.className = 'mk-share-copy';
    copy.textContent = 'Kopiér link';
    copy.setAttribute('aria-label', 'Kopiér link til ' + label);
    copy.addEventListener('click', () => copyLink(copy, chartLink(canvas.id)));
    actions.append(linkedin, copy);
    chartBox.insertAdjacentElement('beforebegin', actions);
  }

  const style = document.createElement('style');
  style.textContent = '#dak-dashboard .mk-linkedin-buttons{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0 12px;align-items:center}' +
    '#dak-dashboard .mk-linkedin-buttons button{font-size:12px;min-height:36px;padding:8px 12px;border-radius:7px;font-weight:700;cursor:pointer;border:1px solid #c9bcae;background:#fff;color:#4a3428}' +
    '#dak-dashboard .mk-linkedin-buttons .mk-share-linkedin{background:#0a66c2;color:white;border-color:#0a66c2}' +
    '#dak-dashboard .mk-linkedin-buttons .mk-share-linkedin:hover{background:#084e96}' +
    '#dak-dashboard .mk-linkedin-buttons .mk-share-copy:hover{background:#f7f1ea}' +
    '#dak-dashboard .mk-linkedin-buttons button:focus-visible{outline:3px solid #146298;outline-offset:2px}' +
    '@media(max-width:480px){#dak-dashboard .mk-linkedin-buttons button{flex:1 1 42%}}';
  document.head.appendChild(style);

  // Restore the period from the LinkedIn share page before the deep-link scroll.
  const requested = new URLSearchParams(location.search).get('months');
  if (requested && ALLOWED_MONTHS.has(requested) && typeof window.setPeriod === 'function') {
    const periodButton = document.querySelector('#dak-dashboard [data-months="' + requested + '"]');
    window.setPeriod(Number(requested), periodButton);
  }
  const root = document.getElementById('dak-dashboard');
  if (root) {
    root.querySelectorAll('.chart-card').forEach(addButtons);
    new MutationObserver(() => root.querySelectorAll('.chart-card').forEach(addButtons))
      .observe(root, {childList:true, subtree:true});
  }
})();
