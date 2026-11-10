/* TradeAI UI helpers: date, market session status, small interactions. */

function getShanghaiParts() {
  const now = new Date();
  const dateFormatter = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    weekday: 'short'
  });
  const timeFormatter = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Shanghai',
    weekday: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false
  });

  const parts = Object.fromEntries(
    timeFormatter.formatToParts(now).map(part => [part.type, part.value])
  );

  return {
    dateText: dateFormatter.format(now),
    weekday: parts.weekday,
    hour: Number(parts.hour),
    minute: Number(parts.minute)
  };
}

function updateMarketState() {
  const stateEl = document.getElementById('market-state');
  const textEl = document.getElementById('market-state-text');
  const dateEl = document.getElementById('today-text');
  if (!stateEl || !textEl) return;

  const { dateText, weekday, hour, minute } = getShanghaiParts();
  if (dateEl) dateEl.textContent = dateText;

  const isWeekday = !['Sat', 'Sun'].includes(weekday);
  const minutes = hour * 60 + minute;
  const morning = minutes >= 9 * 60 + 30 && minutes <= 11 * 60 + 30;
  const afternoon = minutes >= 13 * 60 && minutes <= 15 * 60;

  let label = '已收盘';
  let open = false;

  if (!isWeekday) {
    label = '周末休市';
  } else if (morning || afternoon) {
    label = '交易中';
    open = true;
  } else if (minutes > 11 * 60 + 30 && minutes < 13 * 60) {
    label = '午间休市';
  } else if (minutes < 9 * 60 + 30) {
    label = '未开盘';
  }

  textEl.textContent = label;
  stateEl.classList.toggle('closed', !open);
}

function setupChips() {
  document.querySelectorAll('.filter-bar .chip').forEach(chip => {
    chip.addEventListener('click', () => {
      document.querySelectorAll('.filter-bar .chip').forEach(item => item.classList.remove('active'));
      chip.classList.add('active');
    });
  });
}

function boot() {
  updateMarketState();
  setupChips();
  window.setInterval(updateMarketState, 60 * 1000);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
