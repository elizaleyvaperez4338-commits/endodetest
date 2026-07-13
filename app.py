import subprocess
import sys
import threading
import http.server
import socketserver
import os

# ===== CONFIGURACIÓN =====
PORT = 7860

# ===== 1. SERVIDOR WEB PARA HEALTH CHECK =====
class HealthCheckHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        
        # Usar string normal y luego encode() a bytes
        html_content = """
        <html>
            <head><title>Moviestar Bot</title></head>
            <body style="font-family: Arial; text-align: center; padding: 50px;">
                <h1>🗜️𝐂𝐨𝐦𝐩𝐫𝐞𝐬𝐬 𝐅𝐚𝐬𝐭⚡</h1>
                <p>Bot online ✅ </p>
            </body>
        </html>
        """
        
        self.wfile.write(html_content.encode('utf-8'))
    
    def log_message(self, format, *args):
        pass

def run_web_server():
    try:
        with socketserver.TCPServer(("", PORT), HealthCheckHandler) as httpd:
            print(f"Servidor web en puerto {PORT}")
            httpd.serve_forever()
    except Exception as e:
        print(f"Error: {e}")

# Iniciar servidor web
web_thread = threading.Thread(target=run_web_server, daemon=True)
web_thread.start()
print("Servidor web iniciado")

# ===== 2. EJECUTAR BOT =====
print(" Iniciando bot...")
if os.path.exists("bot.py"):
    subprocess.run([sys.executable, "bot.py"])
else:
    print("No se encuentra bot.py")