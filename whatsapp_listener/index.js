const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const axios = require('axios');

// ───── Config ─────
const INGEST_URL = process.env.INGEST_URL || 'http://listener:8080/ingest';
const TARGET_GROUPS_RAW = process.env.WA_TARGET_GROUPS || '';
const TARGET_GROUPS = TARGET_GROUPS_RAW
    ? TARGET_GROUPS_RAW.split(',').map(g => g.trim().toLowerCase())
    : [];

console.log('🚀 Starting WhatsApp Listener for Rise Real Bali...');
if (TARGET_GROUPS.length > 0) {
    console.log(`📋 Filtering to groups: ${TARGET_GROUPS.join(', ')}`);
} else {
    console.log('📋 No filter set — listening to ALL groups.');
}

// ───── Client ─────
const client = new Client({
    authStrategy: new LocalAuth({ dataPath: '/data/wwebjs_auth' }),
    puppeteer: {
        headless: true,
        executablePath: process.env.PUPPETEER_EXECUTABLE_PATH || '/usr/bin/chromium',
        args: [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
            '--disable-gpu',
            '--no-first-run',
            '--no-zygote',
            '--single-process',
        ],
    },
});

// ───── QR Code ─────
client.on('qr', (qr) => {
    console.log('\n\n📲 SCAN THIS QR CODE WITH YOUR WHATSAPP:\n');
    qrcode.generate(qr, { small: true });
    console.log('\nGo to: WhatsApp → Linked Devices → Link a Device\n');
});

client.on('authenticated', () => {
    console.log('✅ WhatsApp authenticated successfully!');
});

client.on('auth_failure', (msg) => {
    console.error('❌ WhatsApp authentication failed:', msg);
});

client.on('ready', () => {
    console.log('✅ WhatsApp client is ready! Listening for messages...');
});

client.on('disconnected', (reason) => {
    console.warn('⚠️ WhatsApp disconnected:', reason);
    console.log('Restarting in 10 seconds...');
    setTimeout(() => client.initialize(), 10000);
});

// ───── Message handler ─────
client.on('message', async (message) => {
    try {
        // Пропускаем если сообщение не из группы
        if (!message.from.endsWith('@g.us')) return;

        const chat = await message.getChat();
        const chatName = chat.name || 'Unknown Group';
        const text = message.body || '';

        // Пропускаем пустые сообщения и медиа без текста
        if (!text.trim()) return;

        // Фильтрация по названию группы
        if (TARGET_GROUPS.length > 0) {
            const chatNameLower = chatName.toLowerCase();
            const isTarget = TARGET_GROUPS.some(g => chatNameLower.includes(g));
            if (!isTarget) return;
        }

        console.log(`📩 [${chatName}] ${text.substring(0, 80)}...`);

        // Отправляем в Python listener
        const response = await axios.post(INGEST_URL, {
            text: text,
            chat_title: chatName,
            source: 'whatsapp',
        }, {
            timeout: 30000,
        });

        if (response.data.relevant) {
            console.log(`🎯 [${chatName}] Relevant listing found!`);
        }

    } catch (err) {
        if (err.code === 'ECONNREFUSED') {
            console.error('❌ Cannot reach listener service. Is it running?');
        } else {
            console.error('❌ Error processing message:', err.message);
        }
    }
});

// ───── Start ─────
client.initialize();
