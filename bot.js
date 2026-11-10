'use strict';
require('dotenv').config();

const TelegramBot = require('node-telegram-bot-api');
const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

// ================= XATOLARDAN HIMOYA (Global Error Handlers) =================
process.on('unhandledRejection', (e) => console.error('unhandledRejection:', e && (e.stack || e.message)));
process.on('uncaughtException', (e) => console.error('uncaughtException:', e && (e.stack || e.message)));

// ================= WEB SERVER (Render talabi bo'yicha HTTP server) =================
const PORT = Number(process.env.PORT) || 3000;
const HOST = '0.0.0.0';

const server = http.createServer((req, res) => {
  // Render health check (/health, /ping, /)
  res.writeHead(200, {
    'Content-Type': 'text/plain; charset=utf-8',
    'Cache-Control': 'no-cache'
  });
  res.end('OK: Bot is running smoothly on Render!\n');
});

server.listen(PORT, HOST, () => {
  console.log(`🌐 HTTP Web server Render uchun ${HOST}:${PORT} manzilida muvaffaqiyatli ishga tushdi.`);
});

server.on('error', (err) => {
  console.error('🌐 Web server xatosi:', err.message);
});

// ================= SOZLAMALAR (Environment Variables) =================
const token = process.env.BOT_TOKEN || '8479391754:AAFavvM86ZNJsn9VAv_I2krOcGTmBkY2zzY';
const rawAdminId = process.env.ADMIN_ID || '6125160021';
const ADMIN_ID = Number(rawAdminId);
const ADMIN_CARD = process.env.ADMIN_CARD || '8600 0000 0000 0000 (Ism Familiya)';
const GEMINI_API_KEY = (process.env.GEMINI_API_KEY || '').trim();
const CHANNEL_ID = (process.env.CHANNEL_ID || '').trim();
const CHANNEL_LINK = process.env.CHANNEL_LINK || 'https://t.me';
const TIMEOUT_MIN = Number(process.env.TIMEOUT_MIN || 20);

// Baza papkasini xavfsiz sozlash
const DATA_DIR = process.env.DATA_DIR
  ? path.resolve(process.env.DATA_DIR)
  : path.join(__dirname, 'data');

if (!fs.existsSync(DATA_DIR)) {
  fs.mkdirSync(DATA_DIR, { recursive: true });
}

// Token va Admin ID tekshiruvi (Render to'xtab qolmasligi uchun process.exit chaqirilmaydi)
if (!token || token.includes('[BU_YERGA') || !rawAdminId || rawAdminId.includes('[BU_YERGA') || isNaN(ADMIN_ID)) {
  console.error('\n⚠️ DIQQAT: BOT_TOKEN yoki ADMIN_ID noto\'g\'ri! Render Environment Variables bo\'limida to\'g\'ri sozlang.');
}

let bot;
try {
  bot = new TelegramBot(token, {
    polling: {
      interval: 300,
      autoStart: true,
      params: { timeout: 10 }
    }
  });
  bot.on('polling_error', (e) => {
    if (e && e.message && e.message.includes('401')) {
      console.error('❌ XATOLIK: Bot tokeni yaroqsiz (401 Unauthorized)!');
    } else {
      console.error('⚠️ Telegram polling xatosi:', e && e.message);
    }
  });
  console.log('✅ Telegram bot muvaffaqiyatli ulandi va polling boshlandi.');
} catch (e) {
  console.error('❌ TelegramBot obyektini yaratishda xato:', e.message);
  bot = new Proxy({}, {
    get: () => () => {}
  });
}

// Admin kiritish holati (state)
const adminState = {};

// ================= BAZA (JSON, xavfsiz yozish) =================
const DB_FILE = path.join(DATA_DIR, 'deals.json');
const BLACKLIST_FILE = path.join(DATA_DIR, 'blacklist.json');
const RECEIPTS_FILE = path.join(DATA_DIR, 'receipts.json');
const SETTINGS_FILE = path.join(DATA_DIR, 'settings.json');

function loadData(file) {
  if (!fs.existsSync(file)) return {};
  try {
    const raw = fs.readFileSync(file, 'utf8');
    return raw ? JSON.parse(raw) : {};
  } catch (e) {
    console.error(`loadData(${file}) xato:`, e.message);
    return {};
  }
}

function saveData(file, data) {
  const tmp = file + '.' + Date.now() + '.tmp';
  try {
    fs.writeFileSync(tmp, JSON.stringify(data, null, 2), 'utf8');
    if (fs.existsSync(file)) {
      try {
        fs.renameSync(tmp, file);
      } catch (_) {
        fs.copyFileSync(tmp, file);
        fs.unlinkSync(tmp);
      }
    } else {
      fs.renameSync(tmp, file);
    }
  } catch (e) {
    console.error(`saveData(${file}) xato:`, e.message);
    if (fs.existsSync(tmp)) {
      try { fs.unlinkSync(tmp); } catch (_) {}
    }
  }
}

let deals = loadData(DB_FILE);
let blacklist = loadData(BLACKLIST_FILE);
let receipts = loadData(RECEIPTS_FILE);
let settings = loadData(SETTINGS_FILE);

if (!settings.adminCard) {
  settings.adminCard = ADMIN_CARD;
  saveData(SETTINGS_FILE, settings);
}
if (!settings.channelId && CHANNEL_ID) {
  settings.channelId = CHANNEL_ID;
  settings.channelLink = CHANNEL_LINK;
  saveData(SETTINGS_FILE, settings);
}
if (settings.commissionPercent == null) {
  settings.commissionPercent = 10;
  saveData(SETTINGS_FILE, settings);
}

const saveDeals = () => saveData(DB_FILE, deals);

// ================= YORDAMCHI FUNKSIYALAR =================
const FINAL = ['completed', 'cancelled', 'refunded'];

const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

const fmt = (n) => String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');

function send(id, text, opts = {}) {
  return bot.sendMessage(id, text, { parse_mode: 'HTML', ...opts })
    .catch((e) => console.error(`sendMessage(${id}) xato:`, e.message));
}

function nameOf(user) {
  return user.username ? '@' + user.username : (user.first_name || 'Foydalanuvchi');
}

function newDealId() {
  let id;
  do {
    id = 'S' + crypto.randomBytes(4).toString('hex').toUpperCase();
  } while (deals[id]);
  return id;
}

function getDealByUser(userId) {
  return Object.values(deals).find(
    (d) => (d.buyerId === userId || d.sellerId === userId) && !FINAL.includes(d.status)
  );
}

function setStatus(deal, status) {
  deal.status = status;
  deal.updatedAt = Date.now();
  saveDeals();
}

function addLog(deal, role, type, text, senderTag = '') {
  deal.log = deal.log || [];
  deal.log.push({
    t: new Date().toISOString(),
    role,
    type,
    text: text || '',
    sender: senderTag || (role === 'Xaridor' ? deal.buyerUsername : deal.sellerUsername)
  });
  if (deal.log.length > 500) deal.log.shift();
  saveDeals();
}

// 1-chi savdo uchun 0%, keyingi savdolar uchun komissiya foizi (xaridor to'loviga qo'shiladi)
function getCompletedDealsCount(userId) {
  if (!userId) return 0;
  return Object.values(deals).filter(
    (d) => (d.sellerId === userId || d.buyerId === userId) && d.status === 'completed'
  ).length;
}

function calc(deal) {
  const buyerHistory = deal.buyerId ? getCompletedDealsCount(deal.buyerId) : 0;
  const isFirst = buyerHistory === 0;
  const basePercent = Number(settings.commissionPercent != null ? settings.commissionPercent : 10);
  const percent = isFirst ? 0 : basePercent;
  const fee = Math.floor((deal.amount * percent) / 100);
  const totalPay = deal.amount + fee; // Xaridor to'laydigan jami summa
  const payout = deal.amount;          // Sotuvchiga beriladigan to'liq summa

  return { percent, fee, totalPay, payout, isFirst, basePercent };
}

const isAdmin = (id) => Number(id) === ADMIN_ID;

const STATUS_TEXT = {
  created: '⏳ Yaratilmoqda',
  waiting_seller_join: '👤 Sotuvchi qo\'shilishi kutilmoqda',
  waiting_payment: '💳 Xaridor to\'lovi kutilmoqda',
  checking_payment: '🔎 Admin to\'lovni tekshirmoqda',
  in_trade: '🔄 Savdo jarayonida (Faol chat)',
  dispute: '🚨 Nizo (AI va admin ko\'rib chiqmoqda)',
  payout_pending: '💸 Sotuvchiga to\'lov kutilmoqda',
  completed: '✅ Muvaffaqiyatli yakunlangan',
  cancelled: '❌ Bekor qilingan',
  refunded: '↩️ Pul xaridorga qaytarilgan'
};

// ================= KANAL HAVOLASINI AJRATIB OLISH (PARSER) =================
function parseChannelInput(input) {
  const raw = (input || '').trim();
  if (!raw) return null;
  if (raw.toLowerCase() === 'ochirish') {
    return { disable: true };
  }

  let username = '';
  let link = raw;

  if (/^(https?:\/\/)?(www\.)?t\.me\//i.test(raw)) {
    const cleaned = raw.replace(/^(https?:\/\/)?(www\.)?t\.me\//i, '').split('/')[0].split('?')[0].trim();
    if (cleaned.startsWith('+') || cleaned.startsWith('joinchat')) {
      link = raw.startsWith('http') ? raw : `https://${raw}`;
      username = '';
    } else {
      username = '@' + cleaned;
      link = `https://t.me/${cleaned}`;
    }
  } else if (raw.startsWith('@')) {
    username = raw;
    link = `https://t.me/${raw.slice(1)}`;
  } else if (/^[A-Za-z0-9_]{4,32}$/.test(raw)) {
    username = '@' + raw;
    link = `https://t.me/${raw}`;
  } else {
    link = raw.startsWith('http') ? raw : `https://${raw}`;
  }

  return { username, link };
}

// ================= ADMIN BOSHQARUV PANELI =================
function sendAdminPanel(chatId, messageId = null) {
  const currentCard = settings.adminCard || ADMIN_CARD;
  const currentChannel = settings.channelId || CHANNEL_ID || (settings.channelLink ? settings.channelLink : 'Sozlanmagan (Majburiy emas)');
  const currentLink = settings.channelLink || CHANNEL_LINK || 'Sozlanmagan';
  const currentPercent = Number(settings.commissionPercent != null ? settings.commissionPercent : 10);
  const activeCount = Object.values(deals).filter((d) => !FINAL.includes(d.status)).length;
  const bannedCount = Object.keys(blacklist).length;

  const text =
    `👑 <b>BOSH ADMIN BOSHQARUV PANELI</b>\n` +
    `━━━━━━━━━━━━━━━━━━━━\n\n` +
    `💳 <b>Kafolat Karta Raqami:</b>\n` +
    `<code>${esc(currentCard)}</code>\n\n` +
    `💼 <b>Komissiya stavkasi:</b> <b>${currentPercent}%</b> <i>(1-savdo 0% bepul)</i>\n\n` +
    `📢 <b>Majburiy Kanal:</b> <code>${esc(currentChannel)}</code>\n` +
    `🔗 <b>Kanal Havolasi:</b> ${esc(currentLink)}\n\n` +
    `━━━━━━━━━━━━━━━━━━━━\n` +
    `📊 <b>Platforma Statistikasi:</b>\n` +
    `▸ 📂 Faol savdolar: <b>${activeCount} ta</b>\n` +
    `▸ 🚫 Qora ro'yxatda: <b>${bannedCount} ta</b>\n` +
    `━━━━━━━━━━━━━━━━━━━━\n\n` +
    `👇 <b>Boshqarish uchun quyidagi tugmalardan foydalaning:</b>`;

  const reply_markup = {
    inline_keyboard: [
      [{ text: '💳 Karta raqamini o\'zgartirish', callback_data: 'admin:change_card' }],
      [{ text: '💼 Komissiya foizini o\'zgartirish', callback_data: 'admin:change_percent' }],
      [{ text: '📢 Majburiy kanalni sozlash (silka)', callback_data: 'admin:change_channel' }],
      [
        { text: '📂 Faol savdolar', callback_data: 'admin:view_deals' },
        { text: '🚨 Qora ro\'yxat', callback_data: 'admin:view_blacklist' }
      ],
      [{ text: '🔄 Ma\'lumotlarni yangilash', callback_data: 'admin:refresh' }]
    ]
  };

  if (messageId) {
    return bot.editMessageText(text, {
      chat_id: chatId,
      message_id: messageId,
      parse_mode: 'HTML',
      reply_markup
    }).catch(() => {});
  }
  return send(chatId, text, { reply_markup });
}

// ================= KANAL OBUNASI TEKSHIRUVI =================
async function checkSubscription(userId) {
  const targetChannel = (settings.channelId || CHANNEL_ID || '').trim();
  if (!targetChannel) return true;

  try {
    const member = await bot.getChatMember(targetChannel, userId);
    if (!member) return false;
    const okStatuses = ['creator', 'administrator', 'member', 'restricted'];
    return okStatuses.includes(member.status);
  } catch (err) {
    console.error(`checkSubscription(${userId}, ${targetChannel}) xato:`, err.message);
    return false;
  }
}

async function requireSubscription(chatId, userId) {
  if (isAdmin(userId)) return true;

  const targetChannel = (settings.channelId || CHANNEL_ID || '').trim();
  if (!targetChannel) return true;

  const isSub = await checkSubscription(userId);
  if (!isSub) {
    const link = settings.channelLink || CHANNEL_LINK || `https://t.me/${targetChannel.replace('@', '')}`;
    await send(chatId,
      `📢 <b>DIQQAT! KANALGA OBUNA BO'LING</b>\n` +
      `━━━━━━━━━━━━━━━━━━━━\n` +
      `Botning barcha xavfsizlik va kafolat xizmatlaridan to'liq foydalanish uchun rasmiy kanalimizga a'zo bo'lishingiz shart.\n\n` +
      `👇 <b>Quyidagi tugma orqali kanalga o'ting va «✅ Obuna bo'ldim» tugmasini bosing:</b>`,
      {
        reply_markup: {
          inline_keyboard: [
            [{ text: '➕ Kanalga o\'tish', url: link }],
            [{ text: '✅ Obuna bo\'ldim', callback_data: 'sub_check' }]
          ]
        }
      }
    );
    return false;
  }
  return true;
}

// ================= FOYDALANUVCHI ASOSIY /start MENYUSI =================
function sendStartMenu(chatId, user) {
  const dealsCount = getCompletedDealsCount(user.id);
  const currentPercent = Number(settings.commissionPercent != null ? settings.commissionPercent : 10);
  const commissionNote = dealsCount === 0
    ? '🎁 <b>Maxsus imtiyoz:</b> Siz uchun 1-savdo xizmat haqi <b>MUTLAQO BEPUL (0%)!</b>\n<i>(Keyingi savdolarda tovar summasi ustiga ' + currentPercent + '% komissiya qo\'shiladi)</i>'
    : '💼 <b>Xizmat haqi:</b> ' + currentPercent + '% <i>(xaridor to\'loviga qo\'shiladi, sotuvchiga to\'liq puli yetib boradi)</i>';

  return send(chatId,
    `🌟 <b>Assalomu alaykum, ${esc(nameOf(user))}!</b> 🤝\n` +
    `━━━━━━━━━━━━━━━━━━━━\n` +
    `🛡 <b>XAVFSIZ SAVDO (KAFOLAT) PLATFORMASI</b>\n\n` +
    `Akkauntlar, kanallar, valyutalar va raqamli xizmatlarni <b>100% kafolatli</b> hamda xavfsiz sotish va sotib olishingiz mumkin.\n` +
    `━━━━━━━━━━━━━━━━━━━━\n\n` +
    `${commissionNote}\n\n` +
    `📌 <b>Asosiy buyruqlar:</b>\n` +
    `▸ 🚀 /yangi_savdo — Yangi savdo yaratish\n` +
    `▸ ℹ️ /holat — Joriy faol savdo holati\n` +
    `▸ ❌ /bekor — Savdoni bekor qilish (to'lovgacha)\n` +
    `▸ 🚨 /qora_royxat — Firibgarlar ro'yxati\n\n` +
    `━━━━━━━━━━━━━━━━━━━━\n` +
    `💡 <i>Savdo davomida barcha to'lovlar bot kafolat hisobida xavfsiz saqlanadi!</i>`
  );
}

// ================= GEMINI AI BILAN NIZO TAHLILI =================
async function analyzeDisputeWithGemini(deal) {
  const apiKey = (process.env.GEMINI_API_KEY || GEMINI_API_KEY || '').trim();
  if (!apiKey || apiKey.includes('[BU_YERGA')) {
    console.error('Gemini API kaliti topilmadi');
    return null;
  }

  try {
    const logsText = (deal.log || []).map((l) => {
      const senderTag = l.sender || (l.role === 'Xaridor' ? deal.buyerUsername : deal.sellerUsername);
      const uId = l.role === 'Xaridor' ? deal.buyerId : deal.sellerId;
      return `${senderTag} (ID: ${uId}): ${l.text || l.type}`;
    }).join('\n') || 'Yozishma yo\'q';

    const prompt = `
Sen Telegram Kafolat (Escrow) botining qat'iy va odob-axloqni nazorat qiluvchi AI Moderatormisan.
Savdo ma'lumotlari:
- Savdo ID: #${deal.id}
- Summa: ${deal.amount} so'm
- Xaridor: ${deal.buyerUsername} (ID: ${deal.buyerId})
- Sotuvchi: ${deal.sellerUsername} (ID: ${deal.sellerId})

Savdo chati yozishmalari:
${logsText}

Vazifang:
1. Ishtirokchilardan qaysidir biri haqorat qilganmi, so'kinganmi, tahdid solganmi yoki firibgarlik alomatini ko'rsatganmi?
2. Agar qoidabuzarlik bo'lsa: qoidabuzarning Telegram ID raqamini (faqat son) va rolini ko'rsat.
3. Vaziyat bo'yicha qisqa (2-3 jumla) xulosa yoz.
4. Adminga qaror bo'yicha tavsiya ber (refund - pul xaridorga qaytarilsin, release - pul sotuvchiga berilsin, manual - admin o'zi hal qilsin).

Javobni FAQAT valid JSON formatida qaytar (boshqa hech qanday so'zsiz):
{
  "violationDetected": true,
  "violatorId": 123456789,
  "violatorRole": "Sotuvchi",
  "violationReason": "garang deb haqorat qilgan va so'kingan",
  "summary": "Sotuvchi muloqotda haqoratli so'zlar ishlatgan.",
  "recommendation": "refund",
  "recommendationText": "Pul xaridorga qaytarilsin"
}
`;

    const workingModels = ['gemini-3.1-flash-lite', 'gemini-flash-latest'];

    for (const modelName of workingModels) {
      try {
        const url = `https://generativelanguage.googleapis.com/v1beta/models/${modelName}:generateContent?key=${apiKey}`;
        const res = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            contents: [{ parts: [{ text: prompt }] }],
            generationConfig: {
              responseMimeType: 'application/json'
            }
          })
        });

        if (res.status === 200) {
          const data = await res.json();
          const rawText = data.candidates?.[0]?.content?.parts?.[0]?.text;
          if (rawText) {
            return JSON.parse(rawText);
          }
        }
      } catch (innerErr) {
        console.error(`Gemini model ${modelName} xato:`, innerErr.message);
      }
    }
    return null;
  } catch (e) {
    console.error('Gemini nizo tahlil xatosi:', e.message);
    return null;
  }
}

// ================= /start VA /admin =================
bot.onText(/^\/(start|admin)/, async (msg) => {
  if (msg.chat.type !== 'private') return;
  const chatId = msg.chat.id;
  const userId = msg.from.id;

  if (isAdmin(userId)) {
    return sendAdminPanel(chatId);
  }

  if (blacklist[userId]) {
    return send(chatId, '⛔ <b>Siz qora ro\'yxatdasiz!</b> Bot xizmatlaridan foydalana olmaysiz.');
  }

  if (!(await requireSubscription(chatId, userId))) return;

  sendStartMenu(chatId, msg.from);
});

// ================= /qora_royxat =================
bot.onText(/^\/qora_royxat/, (msg) => {
  const list = Object.values(blacklist);
  if (list.length === 0) {
    return send(msg.chat.id, '✅ <b>Qora ro\'yxat hozircha bo\'sh. Barcha savdolar xavfsiz!</b>');
  }
  let text = '🚨 <b>FIRIBGARLAR VA QOIDABUZARLAR RO\'YXATI:</b>\n━━━━━━━━━━━━━━━━━━━━\n\n';
  list.forEach((it, i) => {
    text += `<b>${i + 1}. ${esc(it.username)}</b> (ID: <code>${esc(it.userId)}</code>)\n` +
      `⚠️ Sabab: <i>${esc(it.reason)}</i>\n` +
      `📅 Sana: <code>${esc(it.date)}</code>\n\n`;
  });
  text += `━━━━━━━━━━━━━━━━━━━━\n⚠️ <i>Ushbu foydalanuvchilar bilan savdo qilish qat'iyan taqiqlanadi!</i>`;
  send(msg.chat.id, text);
});

// ================= /holat =================
bot.onText(/^\/holat/, (msg) => {
  if (msg.chat.type !== 'private') return;
  const deal = getDealByUser(msg.from.id);
  if (!deal) {
    return send(msg.chat.id, 'ℹ️ <b>Sizda hozircha faol savdo mavjud emas.</b>\nYangi savdo boshlash uchun: /yangi_savdo');
  }
  const role = deal.buyerId === msg.from.id ? 'Xaridor 🛒' : 'Sotuvchi 💼';
  const { percent, fee, totalPay, payout, isFirst } = calc(deal);

  const feeText = isFirst
    ? `🎁 Xizmat haqi: <b>0% (1-savdo uchun BEPUL 🎉)</b>\n💰 To'lov summasi: <b>${fmt(totalPay)} so'm</b>\n`
    : `💼 Xizmat haqi (${percent}%): <b>${fmt(fee)} so'm</b>\n💰 Jami to'lov: <b>${fmt(totalPay)} so'm</b> <i>(${fmt(payout)} so'm sotuvchiga)</i>\n`;

  send(msg.chat.id,
    `📋 <b>SAVDO MA'LUMOTLARI: #${deal.id}</b>\n` +
    `━━━━━━━━━━━━━━━━━━━━\n` +
    `👤 <b>Sizning rolingiz:</b> ${role}\n` +
    `🛒 <b>Xaridor:</b> ${esc(deal.buyerUsername)}\n` +
    `💼 <b>Sotuvchi:</b> ${esc(deal.sellerUsername || '—')}\n` +
    `💵 <b>Tovar narxi:</b> <b>${fmt(deal.amount)} so'm</b>\n` +
    feeText +
    `━━━━━━━━━━━━━━━━━━━━\n` +
    `📌 <b>Joriy holat:</b>\n<b>${STATUS_TEXT[deal.status] || deal.status}</b>`
  );
});

// ================= /bekor =================
bot.onText(/^\/bekor/, (msg) => {
  if (msg.chat.type !== 'private') return;
  const userId = msg.from.id;

  if (isAdmin(userId) && adminState[userId]) {
    delete adminState[userId];
    send(msg.chat.id, '❎ <b>Amal bekor qilindi.</b>');
    return sendAdminPanel(msg.chat.id);
  }

  const deal = getDealByUser(userId);
  if (!deal) return send(msg.chat.id, 'ℹ️ <b>Faol savdo topilmadi.</b>');

  if (!['created', 'waiting_seller_join', 'waiting_payment'].includes(deal.status)) {
    return send(msg.chat.id,
      '⚠️ <b>Diqqat!</b> To\'lov kafolat hisobiga o\'tkazilgandan so\'ng savdoni o\'zboshimchalik bilan bekor qilib bo\'lmaydi.\n\n' +
      'Muammo bo\'lsa, savdo ichidagi <b>«🚨 Shikoyat»</b> tugmasidan foydalaning yoki adminga murojaat qiling.'
    );
  }
  setStatus(deal, 'cancelled');
  send(msg.chat.id, `❎ <b>Savdo #${deal.id} muvaffaqiyatli bekor qilindi.</b>`);
  const otherId = userId === deal.buyerId ? deal.sellerId : deal.buyerId;
  if (otherId) send(otherId, `❎ <b>Savdo #${deal.id} ikkinchi tomon tomonidan bekor qilindi.</b>`);
});

// ================= /foiz (Admin uchun qulay buyruq) =================
bot.onText(/^\/foiz(?:\s+(\d+))?/, (msg, match) => {
  if (msg.chat.type !== 'private' || !isAdmin(msg.from.id)) return;
  const val = match[1];
  if (!val) {
    const cur = Number(settings.commissionPercent != null ? settings.commissionPercent : 10);
    return send(msg.chat.id, `💼 Joriy komissiya stavkasi: <b>${cur}%</b>\n\nO'zgartirish uchun: <code>/foiz 10</code> (yoki istalgan foiz)`);
  }
  const num = parseInt(val, 10);
  if (isNaN(num) || num < 0 || num > 50) {
    return send(msg.chat.id, '❌ 0 dan 50 gacha bo\'lgan son kiriting. Masalan: <code>/foiz 10</code>');
  }
  settings.commissionPercent = num;
  saveData(SETTINGS_FILE, settings);
  send(msg.chat.id, `✅ <b>Kafolat komissiyasi ${num}% ga o'zgartirildi!</b>`);
  sendAdminPanel(msg.chat.id);
});

// ================= /karta (Admin uchun qulay buyruq) =================
bot.onText(/^\/karta(?:\s+(.+))?/, (msg, match) => {
  if (msg.chat.type !== 'private' || !isAdmin(msg.from.id)) return;
  const val = match[1];
  if (!val) {
    return send(msg.chat.id, `💳 Joriy kafolat kartasi:\n<code>${esc(settings.adminCard || ADMIN_CARD)}</code>\n\nO'zgartirish uchun: <code>/karta 8600 0000 0000 0000 Ism Familiya</code>`);
  }
  if (val.trim().length < 16) {
    return send(msg.chat.id, '❌ Karta ma\'lumotlari kamida 16 ta belgidan iborat bo\'lishi kerak.');
  }
  settings.adminCard = val.trim();
  saveData(SETTINGS_FILE, settings);
  send(msg.chat.id, `✅ <b>Yangi kafolat kartasi saqlandi:</b>\n<code>${esc(settings.adminCard)}</code>`);
  sendAdminPanel(msg.chat.id);
});

// ================= /yangi_savdo =================
bot.onText(/^\/yangi_savdo/, async (msg) => {
  if (msg.chat.type !== 'private') return;
  const chatId = msg.chat.id;
  const userId = msg.from.id;
  if (blacklist[userId]) return;

  if (!(await requireSubscription(chatId, userId))) return;

  const active = getDealByUser(userId);
  if (active) {
    return send(chatId, `⚠️ <b>Sizda tugallanmagan savdo mavjud (#${active.id})!</b>\nAvval uni yakunlang yoki /bekor qiling.`);
  }

  const id = newDealId();
  deals[id] = {
    id,
    buyerId: userId,
    buyerUsername: nameOf(msg.from),
    sellerId: null,
    sellerUsername: null,
    amount: 0,
    status: 'created',
    sellerCard: null,
    createdAt: Date.now(),
    updatedAt: Date.now(),
    log: []
  };
  saveDeals();

  send(chatId,
    `✨ <b>YANGI SAVDO YARATILDI: #${id}</b>\n` +
    `━━━━━━━━━━━━━━━━━━━━\n` +
    `👤 Rolingiz: <b>Xaridor 🛒</b>\n\n` +
    `👉 <b>Sotuvchining Telegram @username manzilini kiriting:</b>\n` +
    `<i>(Masalan: <code>@foydalanuvchi</code>)</i>\n` +
    `━━━━━━━━━━━━━━━━━━━━\n` +
    `❌ <i>Bekor qilish uchun: /bekor</i>`
  );
});

// ================= /qoshilish (sotuvchi) =================
bot.onText(/^\/qoshilish\s+(\S+)/, async (msg, match) => {
  if (msg.chat.type !== 'private') return;
  const chatId = msg.chat.id;
  const userId = msg.from.id;
  if (blacklist[userId]) return;

  if (!(await requireSubscription(chatId, userId))) return;

  const deal = deals[match[1].trim().toUpperCase()];
  if (!deal || deal.status !== 'waiting_seller_join') {
    return send(chatId, '❌ <b>Bunday savdo topilmadi yoki u allaqachon faollashgan!</b>');
  }
  if (deal.buyerId === userId) {
    return send(chatId, '❌ <b>Siz o\'zingiz yaratgan savdoga sotuvchi bo\'lib qo\'shila olmaysiz!</b>');
  }
  if (!msg.from.username || ('@' + msg.from.username).toLowerCase() !== deal.sellerUsername.toLowerCase()) {
    return send(chatId, `❌ <b>Ushbu savdo faqat ${esc(deal.sellerUsername)} uchun yaratilgan!</b>\nSizning username mos kelmadi.`);
  }
  if (getDealByUser(userId)) {
    return send(chatId, '⚠️ <b>Sizda boshqa faol savdo mavjud!</b> Avval uni yakunlang.');
  }

  deal.sellerId = userId;
  setStatus(deal, 'waiting_payment');

  const { percent, fee, totalPay, isFirst } = calc(deal);
  const currentCard = settings.adminCard || ADMIN_CARD;

  // Sotuvchiga xabar: u to'liq narxini oladi
  send(userId,
    `🤝 <b>SIZ SAVDOGA SOTUVCHI SIFATIDA QO'SHILDINGIZ!</b>\n` +
    `━━━━━━━━━━━━━━━━━━━━\n` +
    `🆔 <b>Savdo:</b> <code>#${deal.id}</code>\n` +
    `💵 <b>Tovar narxi:</b> <b>${fmt(deal.amount)} so'm</b>\n` +
    `💰 <b>Savdo yakunlangach sizga to'liq ${fmt(deal.amount)} so'm o'tkazib beriladi!</b>\n` +
    `━━━━━━━━━━━━━━━━━━━━\n\n` +
    `⏳ <b>Xaridor kafolat hisobiga to'lov qilishi kutilmoqda.</b>\n` +
    `⚠️ <i>Hozircha xaridorga hech qanday akkaunt yoki ma'lumot bermang! Pul tushgach bot xabar beradi.</i>`
  );

  // Xaridorga to'lov xabari: summa + komissiya tushuntiriladi
  const paymentDetailNote = isFirst
    ? `📌 <i>Siz kartaga jami <b>${fmt(totalPay)} so'm</b> o'tkazasiz (${fmt(deal.amount)} so'm sotuvchi uchun, 1-savdo bo'lgani sababli komissiya olinmadi 🎉)</i>`
    : `📌 <i>Diqqat! Siz kartaga jami <b>${fmt(totalPay)} so'm</b> o'tkazasiz: <b>${fmt(fee)} so'm</b> kafolat komissiyasi va <b>${fmt(deal.amount)} so'm</b> sotuvchi uchun.</i>`;

  send(deal.buyerId,
    `🎉 <b>Sotuvchi savdoga qo'shildi!</b>\n` +
    `━━━━━━━━━━━━━━━━━━━━\n` +
    `🆔 <b>Savdo:</b> <code>#${deal.id}</code>\n` +
    `💳 <b>Kafolat to'lovi uchun karta:</b>\n` +
    `<code>${esc(currentCard)}</code>\n\n` +
    `💵 <b>Tovar narxi:</b> ${fmt(deal.amount)} so'm\n` +
    (isFirst ? `🎁 <b>Xizmat haqi:</b> 0% (BEPUL)\n` : `💼 <b>Xizmat haqi (${percent}%):</b> ${fmt(fee)} so'm\n`) +
    `💰 <b>JAMI TO'LANADIGAN SUMMA:</b> <b>${fmt(totalPay)} so'm</b>\n` +
    `━━━━━━━━━━━━━━━━━━━━\n\n` +
    `${paymentDetailNote}\n\n` +
    `📸 To'lovni amalga oshirgach, <b>to'lov chekini (rasm yoki fayl ko'rinishida)</b> shu yerga yuboring.\n` +
    `⏱ <i>${TIMEOUT_MIN} daqiqa ichida to'lov qilinmasa, savdo bekor bo'ladi.</i>`
  );
});

// ================= ASOSIY XABAR HANDLERI =================
bot.on('message', async (msg) => {
  try {
    await handleMessage(msg);
  } catch (e) {
    console.error('handleMessage xato:', e);
  }
});

async function handleMessage(msg) {
  if (msg.chat.type !== 'private') return;
  const chatId = msg.chat.id;
  const userId = msg.from.id;
  const text = msg.text;

  // --- ADMIN KARTA, FOIZ VA KANAL KIRITISH HOLATINI USHLASH ---
  if (isAdmin(userId) && adminState[userId] && text && !text.startsWith('/')) {
    const state = adminState[userId];

    if (state === 'waiting_card') {
      if (text.trim().length < 16) {
        return send(chatId, '❌ <b>Karta raqami juda qisqa!</b>\nKamida 16 xonali karta raqami va egasining ismini yozing:\n<i>Bekor qilish: /bekor</i>');
      }
      settings.adminCard = text.trim();
      saveData(SETTINGS_FILE, settings);
      delete adminState[userId];

      send(chatId,
        `✅ <b>KARTA RAQAMI MUOFFAQIYATLI SAQLANDI!</b>\n` +
        `━━━━━━━━━━━━━━━━━━━━\n` +
        `💳 Yangi karta: <code>${esc(settings.adminCard)}</code>`
      );
      return sendAdminPanel(chatId);
    }

    if (state === 'waiting_percent') {
      const num = parseInt(text.replace(/\D/g, ''), 10);
      if (isNaN(num) || num < 0 || num > 50) {
        return send(chatId, '❌ <b>Noto\'g\'ri foiz!</b> 0 dan 50 gacha bo\'lgan butun son kiriting (masalan: <code>10</code> yoki <code>5</code>):\n<i>Bekor qilish: /bekor</i>');
      }
      settings.commissionPercent = num;
      saveData(SETTINGS_FILE, settings);
      delete adminState[userId];

      send(chatId,
        `✅ <b>KOMISSIYA FOIZI MUOFFAQIYATLI YANGILANDI!</b>\n` +
        `━━━━━━━━━━━━━━━━━━━━\n` +
        `💼 Yangi stavka: <b>${settings.commissionPercent}%</b> <i>(1-savdo 0% bepul qoladi)</i>`
      );
      return sendAdminPanel(chatId);
    }

    if (state === 'waiting_channel') {
      const parsed = parseChannelInput(text);

      if (!parsed) {
        return send(chatId, '❌ <b>Iltimos, kanal havolasini kiriting!</b>\n<i>Bekor qilish: /bekor</i>');
      }

      if (parsed.disable) {
        settings.channelId = '';
        settings.channelLink = '';
        saveData(SETTINGS_FILE, settings);
        delete adminState[userId];
        send(chatId, '✅ <b>Majburiy kanal obunasi o\'chirildi!</b>');
        return sendAdminPanel(chatId);
      }

      const targetQuery = parsed.username || parsed.link;
      try {
        const chat = await bot.getChat(targetQuery);
        settings.channelId = chat.username ? '@' + chat.username : String(chat.id);
        settings.channelLink = chat.username ? `https://t.me/${chat.username}` : parsed.link;
      } catch (err) {
        settings.channelId = parsed.username || parsed.link;
        settings.channelLink = parsed.link;
      }

      let adminNotice = '';
      try {
        const me = await bot.getMe();
        const member = await bot.getChatMember(settings.channelId, me.id);
        if (!['administrator', 'creator'].includes(member.status)) {
          adminNotice = '\n\n⚠️ <b>DIQQAT:</b> Bot hozirda bu kanalda ADMIN emas!\nFoydalanuvchilar a\'zoligini bot tekshira olishi uchun botni <b>kanalingizga Administrator (Admin)</b> qilib tayinlang!';
        } else {
          adminNotice = '\n\n✅ <b>Bot kanalda administrator sifatida tasdiqlandi! Obuna tekshiruvi to\'liq faol.</b>';
        }
      } catch (err) {
        adminNotice = '\n\n⚠️ <b>ESLATMA:</b> Bot a\'zolikni tekshira olishi uchun botni <b>kanalingizga ADMIN</b> qilib qo\'shing!';
      }

      saveData(SETTINGS_FILE, settings);
      delete adminState[userId];

      send(chatId,
        `✅ <b>MAJBURiY KANAL SAQLANDI!</b>\n` +
        `━━━━━━━━━━━━━━━━━━━━\n` +
        `📢 <b>Kanal ID:</b> <code>${esc(settings.channelId)}</code>\n` +
        `🔗 <b>Havola:</b> ${esc(settings.channelLink)}` +
        adminNotice
      );
      return sendAdminPanel(chatId);
    }
  }

  if (msg.text && msg.text.startsWith('/')) return;
  if (blacklist[userId]) return;

  const deal = getDealByUser(userId);
  if (!deal) return;

  const isBuyer = deal.buyerId === userId;
  const isSeller = deal.sellerId === userId;

  // 1-qadam: sotuvchi username
  if (deal.status === 'created' && isBuyer && !deal.sellerUsername) {
    if (!text || !/^@[A-Za-z0-9_]{5,32}$/.test(text.trim())) {
      return send(chatId, '❌ <b>Username @ belgisi bilan bo\'lishi kerak</b> (masalan: <code>@foydalanuvchi</code>):');
    }
    if (text.trim().toLowerCase() === deal.buyerUsername.toLowerCase()) {
      return send(chatId, '❌ <b>O\'zingizni sotuvchi qilib ko\'rsata olmaysiz!</b>');
    }
    deal.sellerUsername = text.trim();
    deal.updatedAt = Date.now();
    saveDeals();
    return send(chatId,
      `✅ <b>Sotuvchi belgilandi:</b> <code>${esc(deal.sellerUsername)}</code>\n\n` +
      `💰 <b>Kelishilgan tovar narxini so'mda kiriting:</b>\n` +
      `<i>(Faqat raqamlar bilan, masalan: <code>100000</code>)</i>`
    );
  }

  // 2-qadam: summa
  if (deal.status === 'created' && isBuyer && deal.sellerUsername && deal.amount === 0) {
    const sum = parseInt((text || '').replace(/\D/g, ''), 10);
    if (!sum || sum < 1000) {
      return send(chatId, '❌ <b>Noto\'g\'ri summa!</b> Eng kam savdo summasi 1 000 so\'m.');
    }
    deal.amount = sum;
    setStatus(deal, 'waiting_seller_join');

    const { percent, fee, totalPay, isFirst } = calc(deal);
    const feeText = isFirst
      ? '🎁 <b>Xizmat haqi:</b> 0% <i>(1-savdo uchun MUTLAQO BEPUL!)</i>\n💰 <b>Jami to\'lov:</b> <b>' + fmt(totalPay) + ' so\'m</b>\n'
      : '💼 <b>Xizmat haqi (' + percent + '%):</b> ' + fmt(fee) + ' so\'m\n💰 <b>JAMI TO\'LOV:</b> <b>' + fmt(totalPay) + ' so\'m</b> <i>(' + fmt(fee) + ' so\'m komissiya + ' + fmt(deal.amount) + ' so\'m sotuvchiga)</i>\n';

    return send(chatId,
      `🎉 <b>SAVDO MUVAFFAQIYATLI SAQLANDI!</b>\n` +
      `━━━━━━━━━━━━━━━━━━━━\n` +
      `🆔 <b>Savdo kodi:</b> <code>#${deal.id}</code>\n` +
      `👤 <b>Sotuvchi:</b> ${esc(deal.sellerUsername)}\n` +
      `💵 <b>Tovar narxi:</b> <b>${fmt(deal.amount)} so'm</b>\n` +
      feeText +
      `━━━━━━━━━━━━━━━━━━━━\n\n` +
      `🤝 <b>Sotuvchiga quyidagi buyruqni yuboring:</b>\n` +
      `<code>/qoshilish ${deal.id}</code>\n` +
      `<i>(U botga kirib ushbu buyruqni yuborishi kerak)</i>\n\n` +
      `⏱ <i>Eslatma: ${TIMEOUT_MIN} daqiqa ichida qo'shilmasa, savdo bekor bo'ladi.</i>`
    );
  }

  // 3-qadam: chek (rasm yoki fayl)
  if (deal.status === 'waiting_payment' && isBuyer) {
    let fileId = null, uniqueId = null, kind = null;
    if (msg.photo) {
      const p = msg.photo[msg.photo.length - 1];
      fileId = p.file_id; uniqueId = p.file_unique_id; kind = 'photo';
    } else if (msg.document) {
      fileId = msg.document.file_id; uniqueId = msg.document.file_unique_id; kind = 'document';
    }
    if (!fileId) return send(chatId, '📎 <b>Iltimos, to\'lov chekini rasm yoki fayl ko\'rinishida yuboring!</b>');

    if (receipts[uniqueId]) {
      return send(chatId, '❌ <b>Bu to\'lov cheki avval ishlatilgan!</b> Yangi haqiqiy chek yuboring.');
    }
    receipts[uniqueId] = { dealId: deal.id, date: new Date().toISOString() };
    saveData(RECEIPTS_FILE, receipts);

    setStatus(deal, 'checking_payment');
    send(chatId,
      `📨 <b>Chek muvaffaqiyatli qabul qilindi!</b>\n` +
      `━━━━━━━━━━━━━━━━━━━━\n` +
      `⏳ <i>Admin to'lovni hisobga tushganini tekshirmoqda. Tez orada savdo xonasi ochiladi...</i>`
    );

    const { percent, fee, totalPay, isFirst } = calc(deal);
    const caption =
      `🔔 <b>YANGI TO'LOV CHEKI KELDI!</b>\n` +
      `━━━━━━━━━━━━━━━━━━━━\n` +
      `🆔 <b>Savdo:</b> <code>#${deal.id}</code>\n` +
      `🛒 <b>Xaridor:</b> ${esc(deal.buyerUsername)} (ID: <code>${deal.buyerId}</code>)\n` +
      `💼 <b>Sotuvchi:</b> ${esc(deal.sellerUsername)} (ID: <code>${deal.sellerId}</code>)\n` +
      `💵 <b>Tovar narxi:</b> ${fmt(deal.amount)} so'm\n` +
      (isFirst ? `🎁 <b>Komissiya:</b> 0 so'm (1-savdo bepul)\n` : `💼 <b>Komissiya (${percent}%):</b> ${fmt(fee)} so'm\n`) +
      `💰 <b>KARTAGA TUSHISHI KERAK:</b> <b>${fmt(totalPay)} so'm</b>\n` +
      `<i>(${fmt(deal.amount)} so'm sotuvchiga, ${fmt(fee)} so'm sizning komissiyangiz)</i>\n` +
      `━━━━━━━━━━━━━━━━━━━━\n\n` +
      `Pul kafolat hisobingizga tushdimi?`;

    const opts = {
      caption,
      parse_mode: 'HTML',
      reply_markup: {
        inline_keyboard: [[
          { text: '✅ To\'lovni tasdiqlash', callback_data: `approve:${deal.id}` },
          { text: '❌ Rad etish', callback_data: `reject:${deal.id}` }
        ]]
      }
    };
    const fn = kind === 'photo' ? 'sendPhoto' : 'sendDocument';
    bot[fn](ADMIN_ID, fileId, opts).catch((e) => console.error('Adminga chek yuborilmadi:', e.message));
    return;
  }

  // 4-qadam: sotuvchi karta raqami (yakunlangach)
  if (deal.status === 'payout_pending' && isSeller) {
    const card = (text || '').replace(/\s+/g, '');
    if (!/^\d{16}$/.test(card)) {
      return send(chatId, '💳 <b>Iltimos, 16 xonali karta raqamingizni to\'g\'ri yozing</b> (faqat raqamlar):');
    }
    deal.sellerCard = card;
    deal.updatedAt = Date.now();
    saveDeals();

    const { fee, totalPay } = calc(deal);

    send(chatId,
      `✅ <b>Karta raqamingiz qabul qilindi!</b>\n` +
      `Admin tez orada <b>${fmt(deal.amount)} so'm</b> pulni kartangizga o'tkazib beradi.`
    );

    send(ADMIN_ID,
      `💸 <b>SOTUVCHIGA PUL O'TKAZISH SO'ROVI (#${deal.id})</b>\n` +
      `━━━━━━━━━━━━━━━━━━━━\n` +
      `💼 <b>Sotuvchi:</b> ${esc(deal.sellerUsername)} (ID: <code>${deal.sellerId}</code>)\n` +
      `💳 <b>Karta raqami:</b> <code>${card}</code>\n` +
      `💰 <b>Sotuvchiga o'tkaziladigan summa:</b> <b>${fmt(deal.amount)} so'm</b>\n` +
      (fee > 0 ? `<i>(Xaridordan jami ${fmt(totalPay)} so'm olingan, ${fmt(fee)} so'm sizning komissiyangiz)</i>\n` : '') +
      `━━━━━━━━━━━━━━━━━━━━\n` +
      `<i>Pulni sotuvchiga o'tkazib bergach, pastdagi tugmani bosing:</i>`,
      { reply_markup: { inline_keyboard: [[{ text: '✅ Pul o\'tkazildi (Savdoni yopish)', callback_data: `paid:${deal.id}` }]] } }
    );
    return;
  }

  // 5-qadam: savdo chati (matn, rasm, fayl, video, ovoz)
  if (['in_trade', 'dispute'].includes(deal.status) && (isBuyer || isSeller)) {
    const receiverId = isBuyer ? deal.sellerId : deal.buyerId;
    const role = isBuyer ? 'Xaridor 🛒' : 'Sotuvchi 💼';
    const senderTag = isBuyer ? deal.buyerUsername : deal.sellerUsername;

    if (text) {
      addLog(deal, role, 'text', text, senderTag);
      return send(receiverId, `💬 <b>${role} (${esc(senderTag)}):</b>\n${esc(text)}`);
    }
    const type = msg.photo ? 'photo' : msg.document ? 'document' : msg.video ? 'video' :
      msg.voice ? 'voice' : msg.audio ? 'audio' : null;
    if (!type) return;
    addLog(deal, role, type, msg.caption || `[${type}]`, senderTag);
    send(receiverId, `📎 <b>${role} (${esc(senderTag)}) fayl yubordi:</b>`)
      .then(() => bot.copyMessage(receiverId, chatId, msg.message_id))
      .catch((e) => console.error('copyMessage xato:', e.message));

    // Admin dalil sifatida nusxa oladi
    bot.copyMessage(ADMIN_ID, chatId, msg.message_id, {
      caption: `#${deal.id} | ${role} (${senderTag})${msg.caption ? ': ' + msg.caption : ''}`
    }).catch(() => {});
  }
}

// ================= TUGMALAR (CALLBACK) =================
bot.on('callback_query', async (query) => {
  try {
    await handleCallback(query);
  } catch (e) {
    console.error('callback xato:', e);
    bot.answerCallbackQuery(query.id).catch(() => {});
  }
});

async function handleCallback(query) {
  const fromId = query.from.id;
  const data = query.data || '';
  const answer = (text, alert = false) =>
    bot.answerCallbackQuery(query.id, { text, show_alert: alert }).catch(() => {});

  // --- KANAL OBUNASINI QAYTA TEKSHIRISH ---
  if (data === 'sub_check') {
    const isSub = await checkSubscription(fromId);
    if (isSub) {
      answer('✅ Obuna muvaffaqiyatli tasdiqlandi!', false);
      if (query.message) {
        bot.deleteMessage(query.message.chat.id, query.message.message_id).catch(() => {});
      }
      return sendStartMenu(fromId, query.from);
    } else {
      answer('❌ Siz hali kanalga a\'zo bo\'lmadingiz! Iltimos, avval kanalga obuna bo\'ling.', true);
    }
    return;
  }

  // --- ADMIN BOSHQARUV PANELI TUGMALARI ---
  if (data.startsWith('admin:')) {
    if (!isAdmin(fromId)) return answer('Ruxsat yo\'q');

    const adminAction = data.replace('admin:', '');

    if (adminAction === 'change_card') {
      adminState[fromId] = 'waiting_card';
      answer('Karta kiritish rejimi faollashdi');
      return send(fromId,
        `💳 <b>YANGI KAFOLAT KARTA RAQAMINI KIRITING:</b>\n` +
        `━━━━━━━━━━━━━━━━━━━━\n` +
        `Misol:\n<code>8600 1234 5678 9012 (Ism Familiya)</code>\n\n` +
        `❌ <i>Bekor qilish uchun: /bekor</i>`
      );
    }

    if (adminAction === 'change_percent') {
      adminState[fromId] = 'waiting_percent';
      answer('Komissiya foizini o\'zgartirish rejimi');
      const current = Number(settings.commissionPercent != null ? settings.commissionPercent : 10);
      return send(fromId,
        `💼 <b>KOMISSIYA FOIZINI O'ZGARTIRISH</b>\n` +
        `━━━━━━━━━━━━━━━━━━━━\n` +
        `Joriy stavka: <b>${current}%</b> <i>(1-savdo 0% bepul)</i>\n\n` +
        `Yangi foiz miqdorini raqamda kiriting:\n` +
        `<i>(Masalan: <code>10</code> yoki <code>5</code> yoki <code>0</code>)</i>\n\n` +
        `❌ <i>Bekor qilish uchun: /bekor</i>`
      );
    }

    if (adminAction === 'change_channel') {
      adminState[fromId] = 'waiting_channel';
      answer('Kanal sozlash rejimi faollashdi');
      return send(fromId,
        `📢 <b>MAJBURiY KANAL HAVOLASINI YUBORING:</b>\n` +
        `━━━━━━━━━━━━━━━━━━━━\n` +
        `Faqatgina kanal silkasini yuboring:\n` +
        `Misol: <code>https://t.me/voltabn</code>\n\n` +
        `❌ <i>Obunani butunlay o'chirish uchun: <code>ochirish</code> deb yozing.</i>\n` +
        `<i>Bekor qilish: /bekor</i>`
      );
    }

    if (adminAction === 'view_deals') {
      answer();
      const list = Object.values(deals).filter((d) => !FINAL.includes(d.status));
      if (!list.length) return send(fromId, 'ℹ️ <b>Hozirda faol savdolar mavjud emas.</b>');
      let text = '📂 <b>FAOL SAVDOLAR RO\'YXATI:</b>\n━━━━━━━━━━━━━━━━━━━━\n\n';
      list.forEach((d) => {
        text += `🆔 <b>#${d.id}</b> | <b>${fmt(d.amount)} so'm</b>\n` +
          `📌 Holat: ${STATUS_TEXT[d.status] || d.status}\n` +
          `👥 ${esc(d.buyerUsername)} ➔ ${esc(d.sellerUsername || '—')}\n\n`;
      });
      return send(fromId, text);
    }

    if (adminAction === 'view_blacklist') {
      answer();
      const list = Object.values(blacklist);
      if (!list.length) return send(fromId, '✅ <b>Qora ro\'yxat hozircha bo\'sh.</b>');
      let text = '🚨 <b>QORA RO\'YXATDAGILAR:</b>\n━━━━━━━━━━━━━━━━━━━━\n\n';
      list.forEach((it, i) => {
        text += `<b>${i + 1}. ${esc(it.username)}</b> (ID: <code>${esc(it.userId)}</code>)\n` +
          `⚠️ Sabab: <i>${esc(it.reason)}</i>\n\n`;
      });
      return send(fromId, text);
    }

    if (adminAction === 'refresh') {
      answer('Yangilandi');
      return sendAdminPanel(fromId, query.message ? query.message.message_id : null);
    }

    return answer();
  }

  // --- SAVDO AMALLARI ---
  const [action, dealId] = data.split(':');
  const deal = deals[dealId];

  if (!deal) return answer('Savdo topilmadi', true);
  if (blacklist[fromId]) return answer('Siz bloklangansiz', true);

  const editAdminMsg = (text) => {
    if (!query.message) return;
    const opts = { chat_id: query.message.chat.id, message_id: query.message.message_id };
    const p = query.message.photo || query.message.document
      ? bot.editMessageCaption(text, opts)
      : bot.editMessageText(text, opts);
    p.catch(() => {});
  };

  // --- ADMIN: to'lovni tasdiqlash ---
  if (action === 'approve') {
    if (!isAdmin(fromId)) return answer('Ruxsat yo\'q');
    if (deal.status !== 'checking_payment') return answer('Bu savdo allaqachon ko\'rib chiqilgan', true);
    setStatus(deal, 'in_trade');
    answer('To\'lov tasdiqlandi');
    editAdminMsg(`✅ To'lov tasdiqlandi (#${deal.id})`);

    const tradeMsg =
      `🚀 <b>PUL KAFOLAT HISOBIGA QABUL QILINDI! (#${deal.id})</b>\n` +
      `━━━━━━━━━━━━━━━━━━━━\n` +
      `🔐 <b>Savdo xonasi ochildi!</b>\n\n` +
      `💬 Endi mahsulot ma'lumotlarini (login, parol, fayllar) shu yerda uzatishingiz mumkin.\n` +
      `Bot xabarlarni ikkinchi tomonga xavfsiz yetkazadi.\n` +
      `━━━━━━━━━━━━━━━━━━━━\n` +
      `⚠️ <i>Muammo bo'lsa, pastdagi «🚨 Shikoyat» tugmasini bosing.</i>`;

    send(deal.buyerId, tradeMsg + `\n\n👇 <b>Mahsulotni qabul qilib, to'liq tekshirgach, pastdagi tugmani bosing:</b>`, {
      reply_markup: {
        inline_keyboard: [
          [{ text: '✅ Mahsulotni oldim, pul sotuvchiga berilsin', callback_data: `finish:${deal.id}` }],
          [{ text: '🚨 Shikoyat (AI & Adminni chaqirish)', callback_data: `scam:${deal.id}` }]
        ]
      }
    });
    send(deal.sellerId, tradeMsg, {
      reply_markup: {
        inline_keyboard: [[{ text: '🚨 Shikoyat (AI & Adminni chaqirish)', callback_data: `scam:${deal.id}` }]]
      }
    });
    return;
  }

  // --- ADMIN: rad etish ---
  if (action === 'reject') {
    if (!isAdmin(fromId)) return answer('Ruxsat yo\'q');
    if (deal.status !== 'checking_payment') return answer('Bu savdo allaqachon ko\'rib chiqilgan', true);
    setStatus(deal, 'cancelled');
    answer('Rad etildi');
    editAdminMsg(`❌ To'lov rad etildi (#${deal.id})`);
    send(deal.buyerId, `❌ <b>To'lov cheki tasdiqlanmadi yoki hisobga pul tushmadi.</b>\nSavdo #${deal.id} bekor qilindi.`);
    send(deal.sellerId, `❌ <b>Xaridor to'lovi tasdiqlanmadi.</b>\nSavdo #${deal.id} to'xtatildi.`);
    return;
  }

  // --- XARIDOR: mahsulotni oldim ---
  if (action === 'finish') {
    if (deal.buyerId !== fromId) return answer('Faqat xaridor tasdiqlashi mumkin', true);
    if (deal.status !== 'in_trade') return answer('Bu amal hozir mavjud emas', true);
    setStatus(deal, 'payout_pending');
    answer('Qabul qilindi');
    if (query.message) {
      bot.editMessageReplyMarkup({ inline_keyboard: [] },
        { chat_id: query.message.chat.id, message_id: query.message.message_id }).catch(() => {});
    }
    send(deal.buyerId,
      `✨ <b>Savdo muvaffaqiyatli yakunlandi!</b>\n` +
      `Kafolat xizmatimizdan foydalanganingiz uchun rahmat! 🤝`
    );
    send(deal.sellerId,
      `🎉 <b>XARIDOR MAHSULOTNI QABUL QILDI!</b>\n` +
      `━━━━━━━━━━━━━━━━━━━━\n` +
      `💰 Sizga o'tkaziladigan summa: <b>${fmt(deal.amount)} so'm</b>\n\n` +
      `💳 Iltimos, pulni qabul qilish uchun <b>16 xonali karta raqamingizni</b> shu yerga yozing:`
    );
    send(ADMIN_ID, `ℹ️ <b>Savdo #${deal.id}:</b> Xaridor tasdiqladi. Sotuvchidan karta raqami kutilmoqda.`);
    return;
  }

  // --- SHIKOYAT: AI MODERATOR BILAN TAHLIL QILISH VA CHAT TARIXINI DARHOL YUBORISH ---
  if (action === 'scam') {
    if (deal.buyerId !== fromId && deal.sellerId !== fromId) return answer('Ruxsat yo\'q');
    if (deal.status !== 'in_trade') return answer('Bu amal hozir mavjud emas', true);
    setStatus(deal, 'dispute');
    answer('Shikoyat qabul qilindi. AI moderator tahlil qilmoqda...', true);

    const role = deal.buyerId === fromId ? 'Xaridor 🛒' : 'Sotuvchi 💼';
    const other = deal.buyerId === fromId ? deal.sellerId : deal.buyerId;

    send(fromId, '🚨 <b>Shikoyatingiz qabul qilindi!</b> AI Moderator va admin yozishmalarni tahlil qilmoqda.');
    send(other, '⚠️ <b>Ikkinchi tomon shikoyat qildi!</b> Savdo to\'xtatildi, AI va admin tekshiradi.');

    let chatHistoryText = '';
    if (!deal.log || deal.log.length === 0) {
      chatHistoryText = '<i>(Savdo chatida yozishmalar mavjud emas)</i>';
    } else {
      chatHistoryText = deal.log.slice(-30).map((l) => {
        const senderTag = l.sender || (l.role === 'Xaridor' ? deal.buyerUsername : deal.sellerUsername);
        const content = l.text ? esc(l.text) : `[${l.type}]`;
        return `<b>${esc(senderTag)}:</b> ${content}`;
      }).join('\n');
    }

    const aiResult = await analyzeDisputeWithGemini(deal);

    let aiReport = '🤖 <i>AI tahlili vaqtinchalik javob bermadi. Yozishmalarni o\'zingiz tekshiring.</i>';
    let autoBanNote = '';

    if (aiResult) {
      if (aiResult.violationDetected && aiResult.violatorId) {
        const violatorNum = Number(aiResult.violatorId);
        if (violatorNum && !blacklist[violatorNum]) {
          const vUser = violatorNum === deal.buyerId ? deal.buyerUsername : deal.sellerUsername;
          const vRole = aiResult.violatorRole || (violatorNum === deal.buyerId ? 'Xaridor' : 'Sotuvchi');

          blacklist[violatorNum] = {
            userId: violatorNum,
            username: vUser,
            reason: `AI Moderator: ${aiResult.violationReason || 'So\'kinish/haqorat'}`,
            date: new Date().toLocaleDateString('en-GB')
          };
          saveData(BLACKLIST_FILE, blacklist);

          autoBanNote = `\n\n🚫 <b>AVTOMATIK BLOKLANDI:</b> ${vRole} (${esc(vUser)}, ID: <code>${violatorNum}</code>) qora ro'yxatga kiritildi!`;

          send(violatorNum,
            `⛔ <b>AI Moderator Qarori:</b>\n` +
            `Savdo davomida odob-axloq qoidalarini buzganingiz (<b>${esc(aiResult.violationReason)}</b>) sababli AI tomonidan qora ro'yxatga kiritildingiz va bloklandingiz!`
          );
        }
      }

      aiReport =
        `🤖 <b>AI MODERATOR XULOSASI:</b>\n` +
        `⚠️ Qoidabuzarlik: <b>${aiResult.violationDetected ? 'ANIQLANDI 🚨' : 'Aniqlanmadi ✅'}</b>\n` +
        (aiResult.violationReason ? `📌 Sabab: <i>${esc(aiResult.violationReason)}</i>\n` : '') +
        `📝 Tahlil: <i>${esc(aiResult.summary)}</i>\n` +
        `💡 Tavsiya: <b>${esc(aiResult.recommendationText || aiResult.recommendation)}</b>` +
        autoBanNote;
    }

    const { fee, totalPay } = calc(deal);

    send(ADMIN_ID,
      `🚨 <b>YANGI SHIKOYAT KELIB TUSHDI! (#${deal.id})</b>\n` +
      `━━━━━━━━━━━━━━━━━━━━\n` +
      `👤 <b>Kimdan:</b> ${role} ${esc(nameOf(query.from))} (ID: <code>${fromId}</code>)\n` +
      `🛒 <b>Xaridor:</b> ${esc(deal.buyerUsername)} (ID: <code>${deal.buyerId}</code>)\n` +
      `💼 <b>Sotuvchi:</b> ${esc(deal.sellerUsername)} (ID: <code>${deal.sellerId}</code>)\n` +
      `💵 <b>Tovar narxi:</b> <b>${fmt(deal.amount)} so'm</b>\n` +
      `💰 <b>Xaridor to'lagan jami:</b> <b>${fmt(totalPay)} so'm</b> (Komissiya: ${fmt(fee)} so'm)\n` +
      `━━━━━━━━━━━━━━━━━━━━\n\n` +
      `💬 <b>SAVDO CHATI YOZISHMALARI:</b>\n` +
      `━━━━━━━━━━━━━━━━━━━━\n` +
      `${chatHistoryText}\n` +
      `━━━━━━━━━━━━━━━━━━━━\n\n` +
      aiReport,
      {
        reply_markup: {
          inline_keyboard: [
            [{ text: `↩️ Xaridorga qaytarish (${fmt(totalPay)} so'm)`, callback_data: `refund:${deal.id}` }],
            [{ text: `➡️ Sotuvchiga berish (${fmt(deal.amount)} so'm)`, callback_data: `release:${deal.id}` }]
          ]
        }
      }
    );
    return;
  }

  // --- ADMIN: nizoni hal qilish ---
  if (action === 'refund') {
    if (!isAdmin(fromId)) return answer('Ruxsat yo\'q');
    if (!['dispute', 'in_trade', 'checking_payment'].includes(deal.status)) return answer('Amal mavjud emas', true);
    setStatus(deal, 'refunded');
    answer('Qaytarish belgilandi');
    const { totalPay } = calc(deal);
    editAdminMsg(`↩️ #${deal.id}: Pul xaridorga qaytariladi. (Qo'lda o'tkazing: ${fmt(totalPay)} so'm)`);
    send(deal.buyerId, `↩️ <b>Savdo #${deal.id} bo'yicha pulingiz (${fmt(totalPay)} so'm) sizga qaytariladi.</b>\nAdmin tez orada bog'lanadi.`);
    send(deal.sellerId, `ℹ️ <b>Savdo #${deal.id} admin qarori bilan yopildi</b> (pul xaridorga qaytarildi).`);
    return;
  }

  if (action === 'release') {
    if (!isAdmin(fromId)) return answer('Ruxsat yo\'q');
    if (deal.status !== 'dispute') return answer('Amal mavjud emas', true);
    setStatus(deal, 'payout_pending');
    answer('Sotuvchiga o\'tkazish belgilandi');
    editAdminMsg(`➡️ #${deal.id}: Pul sotuvchiga beriladi.`);
    send(deal.buyerId, `ℹ️ <b>Savdo #${deal.id} admin qarori bilan sotuvchi foydasiga hal qilindi.</b>`);
    send(deal.sellerId, `✅ <b>Savdo #${deal.id} sizning foydangizga hal qilindi!</b>\nSizga <b>${fmt(deal.amount)} so'm</b> to'lanadi.\n💳 16 xonali karta raqamingizni yozing:`);
    return;
  }

  // --- ADMIN: pul o'tkazildi ---
  if (action === 'paid') {
    if (!isAdmin(fromId)) return answer('Ruxsat yo\'q');
    if (deal.status !== 'payout_pending') return answer('Amal mavjud emas', true);
    setStatus(deal, 'completed');
    answer('Yakunlandi');
    editAdminMsg(`✅ #${deal.id}: Pul sotuvchiga o'tkazildi. Savdo yopildi.`);
    send(deal.sellerId, `💸 <b>Savdo #${deal.id}: ${fmt(deal.amount)} so'm pul kartangizga o'tkazildi!</b> Rahmat!`);
    return;
  }

  answer();
}

// ================= ADMIN BUYRUQLARI =================

// /karta — Karta raqamini ko'rish va o'zgartirish
bot.onText(/^\/karta(?:\s+(.+))?/, (msg, match) => {
  if (!isAdmin(msg.from.id)) return;
  const newCard = match[1];

  if (!newCard) {
    const current = settings.adminCard || ADMIN_CARD;
    return send(ADMIN_ID,
      `💳 <b>JORIY KAFOLAT KARTA RAQAMI:</b>\n<code>${esc(current)}</code>\n\n` +
      `O'zgartirish uchun:\n<code>/karta 8600 0000 0000 0000 (Ism Familiya)</code>`
    );
  }

  settings.adminCard = newCard.trim();
  saveData(SETTINGS_FILE, settings);
  send(ADMIN_ID, `✅ <b>Karta raqami yangilandi:</b>\n<code>${esc(settings.adminCard)}</code>`);
});

// /foiz — Komissiya foizini ko'rish va o'zgartirish
bot.onText(/^\/foiz(?:\s+(\d+))?/, (msg, match) => {
  if (!isAdmin(msg.from.id)) return;
  const num = match[1] != null ? parseInt(match[1], 10) : null;

  if (num === null || isNaN(num)) {
    const current = Number(settings.commissionPercent != null ? settings.commissionPercent : 10);
    return send(ADMIN_ID,
      `💼 <b>JORIY KOMISSIYA STAVKASI:</b> <b>${current}%</b>\n\n` +
      `O'zgartirish uchun misol:\n<code>/foiz 10</code> yoki <code>/foiz 5</code>`
    );
  }

  if (num < 0 || num > 50) {
    return send(ADMIN_ID, '❌ Foiz 0 dan 50 gacha bo\'lishi kerak.');
  }

  settings.commissionPercent = num;
  saveData(SETTINGS_FILE, settings);
  send(ADMIN_ID, `✅ <b>Komissiya foizi yangilandi:</b> <b>${settings.commissionPercent}%</b>`);
});

// /kanal — Majburiy kanalni sozlash
bot.onText(/^\/kanal(?:\s+(.+))?/, async (msg, match) => {
  if (!isAdmin(msg.from.id)) return;
  const input = match[1];

  if (!input) {
    return send(ADMIN_ID,
      `📢 <b>MAJBURiY KANAL SOZLAMALARI:</b>\n` +
      `Kanal ID: <code>${esc(settings.channelId || CHANNEL_ID || 'Sozlanmagan')}</code>\n` +
      `Havola: <code>${esc(settings.channelLink || CHANNEL_LINK || 'Sozlanmagan')}</code>\n\n` +
      `O'zgartirish uchun silkani yuboring:\n<code>/kanal https://t.me/mening_kanalim</code>`
    );
  }

  const parsed = parseChannelInput(input);
  if (!parsed) return;

  if (parsed.disable) {
    settings.channelId = '';
    settings.channelLink = '';
    saveData(SETTINGS_FILE, settings);
    return send(ADMIN_ID, '✅ <b>Majburiy kanal obunasi o\'chirildi!</b>');
  }

  const targetQuery = parsed.username || parsed.link;
  try {
    const chat = await bot.getChat(targetQuery);
    settings.channelId = chat.username ? '@' + chat.username : String(chat.id);
    settings.channelLink = chat.username ? `https://t.me/${chat.username}` : parsed.link;
  } catch (err) {
    settings.channelId = parsed.username || parsed.link;
    settings.channelLink = parsed.link;
  }
  saveData(SETTINGS_FILE, settings);

  let notice = '';
  try {
    const me = await bot.getMe();
    const member = await bot.getChatMember(settings.channelId, me.id);
    if (!['administrator', 'creator'].includes(member.status)) {
      notice = '\n\n⚠️ <b>DIQQAT:</b> Bot bu kanalda ADMIN emas! Bot a\'zolikni tekshirishi uchun botni kanalingizga ADMIN qiling!';
    } else {
      notice = '\n\n✅ <b>Bot kanalda administrator sifatida tasdiqlandi!</b>';
    }
  } catch (e) {
    notice = '\n\n⚠️ <b>ESLATMA:</b> Botni kanalingizga ADMIN qilib qo\'shing!';
  }

  send(ADMIN_ID, `✅ <b>Kanal yangilandi:</b> ${esc(settings.channelLink)}${notice}`);
});

// /ban ID sabab
bot.onText(/^\/ban\s+(\d+)\s+(.+)/, (msg, match) => {
  if (!isAdmin(msg.from.id)) return;
  const targetId = Number(match[1]);
  const reason = match[2];

  let username = 'Noma\'lum';
  const deal = getDealByUser(targetId);
  if (deal) username = targetId === deal.buyerId ? deal.buyerUsername : deal.sellerUsername;

  blacklist[targetId] = {
    userId: targetId,
    username,
    reason,
    date: new Date().toLocaleDateString('en-GB')
  };
  saveData(BLACKLIST_FILE, blacklist);

  if (deal) {
    const other = targetId === deal.buyerId ? deal.sellerId : deal.buyerId;
    if (['created', 'waiting_seller_join', 'waiting_payment'].includes(deal.status)) {
      setStatus(deal, 'cancelled');
      if (other) send(other, `❎ <b>Savdo #${deal.id} bekor qilindi (ikkinchi tomon bloklandi).</b>`);
    } else {
      setStatus(deal, 'dispute');
      if (other) send(other, `⚠️ <b>Savdo #${deal.id}:</b> Ikkinchi tomon bloklandi. Admin siz bilan bog'lanadi.`);
      send(ADMIN_ID, `⚠️ <b>#${deal.id} savdosi nizoga o'tdi.</b>`, {
        reply_markup: {
          inline_keyboard: [
            [{ text: '↩️ Pulni xaridorga qaytarish', callback_data: `refund:${deal.id}` }],
            [{ text: '➡️ Pulni sotuvchiga berish', callback_data: `release:${deal.id}` }]
          ]
        }
      });
    }
  }

  send(ADMIN_ID, `🚫 <code>${targetId}</code> <b>qora ro'yxatga kiritildi.</b>`);
  send(targetId, '⛔ <b>Siz qoidalarni buzganingiz sababli butunlay bloklandingiz!</b>');
});

// /unban ID
bot.onText(/^\/unban\s+(\d+)/, (msg, match) => {
  if (!isAdmin(msg.from.id)) return;
  if (!blacklist[match[1]]) return send(ADMIN_ID, 'Bu ID qora ro\'yxatda yo\'q.');
  delete blacklist[match[1]];
  saveData(BLACKLIST_FILE, blacklist);
  send(ADMIN_ID, `✅ <code>${esc(match[1])}</code> <b>qora ro'yxatdan chiqarildi.</b>`);
});

// /savdolar — faol savdolar
bot.onText(/^\/savdolar/, (msg) => {
  if (!isAdmin(msg.from.id)) return;
  const list = Object.values(deals).filter((d) => !FINAL.includes(d.status));
  if (!list.length) return send(ADMIN_ID, 'Faol savdolar yo\'q.');
  let text = '📂 <b>FAOL SAVDOLAR:</b>\n━━━━━━━━━━━━━━━━━━━━\n\n';
  list.forEach((d) => {
    const { totalPay } = calc(d);
    text += `🆔 <b>#${d.id}</b> | Tovar: <b>${fmt(d.amount)} so'm</b> (Jami: ${fmt(totalPay)} so'm)\n` +
      `📌 ${STATUS_TEXT[d.status] || d.status}\n` +
      `👥 ${esc(d.buyerUsername)} ➔ ${esc(d.sellerUsername || '—')}\n\n`;
  });
  send(ADMIN_ID, text);
});

// /savdo ID — yozishmalar tarixi (dalil)
bot.onText(/^\/savdo\s+(\S+)/, (msg, match) => {
  if (!isAdmin(msg.from.id)) return;
  const d = deals[match[1].toUpperCase()];
  if (!d) return send(ADMIN_ID, 'Savdo topilmadi.');
  const { totalPay, fee } = calc(d);
  let text =
    `📋 <b>SAVDO YOZISHMALARI: #${d.id}</b>\n` +
    `━━━━━━━━━━━━━━━━━━━━\n` +
    `🛒 <b>Xaridor:</b> ${esc(d.buyerUsername)} (<code>${d.buyerId}</code>)\n` +
    `💼 <b>Sotuvchi:</b> ${esc(d.sellerUsername)} (<code>${d.sellerId}</code>)\n` +
    `💵 <b>Tovar narxi:</b> <b>${fmt(d.amount)} so'm</b>\n` +
    `💰 <b>Xaridor to'lagan jami:</b> <b>${fmt(totalPay)} so'm</b> (Komissiya: ${fmt(fee)} so'm)\n` +
    `━━━━━━━━━━━━━━━━━━━━\n` +
    `💬 <b>Oxirgi 30 ta xabar:</b>\n\n`;
  (d.log || []).slice(-30).forEach((l) => {
    const sender = l.sender || l.role;
    text += `[${l.t.slice(11, 16)}] <b>${esc(sender)}:</b> ${esc(l.text || l.type)}\n`;
  });
  send(ADMIN_ID, text.slice(0, 4000));
});

// ================= VAQT CHEGARASI (har 1 daqiqada tekshiradi) =================
setInterval(() => {
  const now = Date.now();
  Object.values(deals).forEach((d) => {
    if (!['created', 'waiting_seller_join', 'waiting_payment'].includes(d.status)) return;
    if (now - d.updatedAt < TIMEOUT_MIN * 60 * 1000) return;

    setStatus(d, 'cancelled');
    const cancelMsg =
      `⌛ <b>SAVDO VAQTI TUGADI! (#${d.id})</b>\n` +
      `━━━━━━━━━━━━━━━━━━━━\n` +
      `Ushbu savdo <b>${TIMEOUT_MIN} daqiqa</b> ichida to'lov qilinmagani / davom ettirilmagani sababli avtomatik tarzda bekor qilindi.\n\n` +
      `Qayta savdo boshlash uchun: /yangi_savdo buyrug'ini bosing.`;

    send(d.buyerId, cancelMsg);
    if (d.sellerId) send(d.sellerId, cancelMsg);
  });
}, 60 * 1000);

console.log('✅ Kafolat boti yangi komissiya tizimi bilan muvaffaqiyatli ishga tushdi!');
