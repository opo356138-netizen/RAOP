import subprocess
import sys
import time
from pathlib import Path
import tkinter as tk

def create_splash():
    """إنشاء شاشة ترحيبية مؤقتة تحتوي على اسم OP في المنتصف"""
    root = tk.Tk()
    root.overrideredirect(True)  # إزالة شريط العنوان والأزرار لتظهر بشكل احترافي
    root.geometry("350x220")
    root.configure(bg="#0f0f0f")
    
    # توسيع النافذة في منتصف الشاشة تماماً
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    x = (sw - 350) // 2
    y = (sh - 220) // 2
    root.geometry(f"350x220+{x}+{y}")
    
    # تصميم إطار داخلي جذاب
    frame = tk.Frame(root, bg="#181818", bd=2, relief="solid")
    frame.place(relx=0.02, rely=0.02, relwidth=0.96, relheight=0.96)
    
    # النص الكبير "OP" في المنتصف بلون مميز
    lbl_op = tk.Label(frame, text="OP", font=("Helvetica", 65, "bold"), fg="#00ffcc", bg="#181818")
    lbl_op.pack(pady=(25, 5))
    
    # نص توضيحي هادئ أسفل الشعار
    lbl_status = tk.Label(frame, text="Sovereign Environment Loading...", font=("Arial", 9), fg="#888888", bg="#181818")
    lbl_status.pack()
    
    root.update()
    return root

def main():
    # تحديد مسار المشروع بدقة
    if getattr(sys, 'frozen', False):
        project_dir = Path(sys.executable).resolve().parent
        python_cmd = "python"
    else:
        project_dir = Path(__file__).resolve().parent
        python_cmd = sys.executable
    
    server_script = project_dir / "server.py"
    client_script = project_dir / "topfra_client.py"
    
    if not server_script.exists() or not client_script.exists():
        print("[Error]: Missing 'server.py' or 'topfra_client.py'!")
        sys.exit(1)

    # 1. إظهار شاشة البداية (Splash Screen) فوراً عند النقر على البرنامج
    splash = None
    try:
        splash = create_splash()
    except Exception:
        pass

    hidden_flags = 0
    if sys.platform == "win32":
        hidden_flags = subprocess.CREATE_NO_WINDOW

    # 2. تشغيل السيرفر في الخلفية بصمت تام
    try:
        server_process = subprocess.Popen(
            [python_cmd, "server.py"], 
            cwd=project_dir,
            creationflags=hidden_flags
        )
    except Exception as e:
        if splash: splash.destroy()
        sys.exit(1)
    
    # الانتظار بسلاسة مع الحفاظ على استجابة شاشة البداية
    start_time = time.time()
    while time.time() - start_time < 2.5:
        if splash:
            splash.update()
        time.sleep(0.1)
    
    # 3. تشغيل واجهة العميل
    try:
        client_process = subprocess.Popen(
            [python_cmd, "topfra_client.py"], 
            cwd=project_dir,
            creationflags=hidden_flags
        )
    except Exception as e:
        server_process.terminate()
        if splash: splash.destroy()
        sys.exit(1)

    # إغلاق شاشة البداية بمجرد ظهور الواجهة الحقيقية
    if splash:
        splash.destroy()

    # إبقاء البرنامج قيد التشغيل لضمان استقرار السيرفر حتى إغلاق الواجهة
    try:
        client_process.wait()
    except KeyboardInterrupt:
        pass
    
    server_process.terminate()

if __name__ == "__main__":
    main()