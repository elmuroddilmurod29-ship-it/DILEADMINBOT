// Kafil Garant Escrow Bot Launcher
const { spawn } = require('child_process');

console.log('Starting Python Escrow Bot (bot.py)...');
const pythonCmd = process.platform === 'win32' ? 'python' : 'python3';
const proc = spawn(pythonCmd, ['bot.py'], { stdio: 'inherit' });

proc.on('close', (code) => {
  process.exit(code || 0);
});
