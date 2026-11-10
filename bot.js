// Kafil Garant Escrow Bot Launcher
const { spawn } = require('child_process');

console.log('Starting Python Escrow Bot (main.py)...');
const pythonCmd = process.platform === 'win32' ? 'python' : 'python3';
const proc = spawn(pythonCmd, ['main.py'], { stdio: 'inherit' });

proc.on('close', (code) => {
  process.exit(code || 0);
});
