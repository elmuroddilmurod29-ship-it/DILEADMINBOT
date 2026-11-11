"""
MAIN.PY - Render va Serverlar uchun Asosiy Kirish Nuqtasi
Ushbu fayl bot.py dagi asosiy asinxron main() funksiyasini ishga tushiradi.
"""
import asyncio
import sys
import os

# Papka yo'lini to'g'ri ulash
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot import main

if __name__ == "__main__":
    asyncio.run(main())
