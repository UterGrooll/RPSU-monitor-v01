import os
import telnetlib
import re
import time
import csv
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from datetime import datetime, timedelta
import threading
import pystray
from PIL import Image, ImageDraw
import sys
import json

# Глобальные переменные
devices = []
MAX_DEVICES = 5
polling_interval = 60  # в минутах
window = None
utc_enabled = None


def save_devices_to_file():
    with open("devices.json", "w") as file:
        json.dump(devices, file)


def load_devices_from_file():
    try:
        with open("devices.json", "r") as file:
            return json.load(file)
    except FileNotFoundError:
        return []


def connect_to_device(ip, port):
    try:
        tn = telnetlib.Telnet(ip, port, timeout=5)
        return tn
    except Exception as e:
        print(f"[{ip}] Connection failed: {e}")
        return None


def send_command(tn, command, delay=1):
    try:
        tn.write(command.encode('ascii') + b"\r\n")
        time.sleep(delay)
        response = tn.read_very_eager().decode('ascii', errors='ignore')
        return response
    except Exception as e:
        print(f"Command '{command}' failed: {e}")
        return ""


def clean_response(response):
    # Удаляем ANSI escape sequences (цвета и т.п.)
    response = re.sub(r'\x1B\[[0-9;]*[a-zA-Z]', '', response)
    # Оставляем только печатаемые ASCII + переводы строк
    response = re.sub(r'[^\x20-\x7E\n\r]', '', response)
    return response.strip()


# --- ИЗВЛЕЧЕНИЕ ПАРАМЕТРОВ ---
def extract_value(response, key):
    match = re.search(rf"{re.escape(key)}\s*[:=]\s*([-\d.]+)", response, re.IGNORECASE)
    return match.group(1) if match else "0"


def extract_uptime(response):
    match = re.search(r"RPSU Uptime[:=]\s*(\d+)", response, re.IGNORECASE)
    return match.group(1) if match else "0"


def extract_rpsu_status(response):
    match = re.search(r"RPSU Status[:=]\s*(ON|OFF)", response, re.IGNORECASE)
    return match.group(1).upper() if match else "OFF"


# 🔥 Ключевое исправление: извлекаем температуру в формате "Temperature : 31.250 C"
def extract_temperature(response):
    # Ищем: "Temperature", затем любые пробелы/двоеточие, затем число (возможно с точкой), затем пробел и C
    # Пример: "Temperature : 31.250 C"
    match = re.search(r"Temperature\s*[:=]\s*([0-9.]+)\s*C", response, re.IGNORECASE)
    if match:
        try:
            # Приводим к float и обратно к строке — чтобы убрать лишние нули, но сохранить как строку
            val = float(match.group(1))
            return f"{val:.1f}"  # Округляем до 1 знака: 31.250 → 31.2
        except ValueError:
            pass
    return "0.0"


# --- ЗАПИСЬ В CSV ---
def write_to_csv(device_name, rpsu_status, rpsu_uptime, voltage, current, leak_current, temperature):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    filename = f"{device_name}_data.csv"
    file_exists = os.path.exists(filename)

    with open(filename, mode="a", newline="", encoding="utf-8") as file:
        writer = csv.writer(file, delimiter=";")
        if not file_exists:
            writer.writerow(["Timestamp", "Status", "Uptime", "Voltage", "Current", "Leak Current", "Temperature"])
        writer.writerow([timestamp, rpsu_status, rpsu_uptime, voltage, current, leak_current, temperature])
    print(f"[{device_name}] Data saved")


def write_to_utc_csv(device_name, rpsu_status, rpsu_uptime, voltage, current, leak_current, temperature):
    timestamp_utc = (datetime.now() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    status_numeric = 1 if rpsu_status == "ON" else 0
    if status_numeric == 0:
        rpsu_uptime = voltage = current = leak_current = "0"

    filename = f"{device_name}_utc_data.csv"
    file_exists = os.path.exists(filename)

    with open(filename, mode="a", newline="", encoding="utf-8") as file:
        writer = csv.writer(file, delimiter=";")
        if not file_exists:
            writer.writerow(["Timestamp", "Status", "Uptime", "Voltage", "Current", "Leak Current", "Temperature"])
        writer.writerow([timestamp_utc, status_numeric, rpsu_uptime, voltage, current, leak_current, temperature])
    print(f"[{device_name}] UTC data saved")


def get_last_data_from_csv(device_name):
    try:
        filename = f"{device_name}_data.csv"
        if not os.path.exists(filename):
            return "", "", "", "", "", ""

        with open(filename, 'r', encoding='utf-8') as f:
            reader = list(csv.reader(f, delimiter=';'))
            if len(reader) > 1:
                last = reader[-1]
                return (
                    last[1] if len(last) > 1 else "",
                    last[2] if len(last) > 2 else "",
                    last[3] if len(last) > 3 else "",
                    last[4] if len(last) > 4 else "",
                    last[5] if len(last) > 5 else "",
                    last[6] if len(last) > 6 else ""
                )
    except Exception as e:
        print(f"CSV read error: {e}")
    return "", "", "", "", "", ""


# --- ОСНОВНАЯ ЛОГИКА ОПРОСА ---
def device_monitoring(device, status_var, uptime_var, voltage_var, current_var, leak_current_var, temperature_var, temperature_label):
    global polling_interval, window
    ip = device["ip"]
    name = device["name"]

    while True:
        tn = connect_to_device(ip, device["port"])
        if not tn:
            status_var.set("Нет связи")
            if window:
                window.after(0, lambda: temperature_label.config(fg="black"))
            time.sleep(polling_interval * 60)
            continue

        temperature = "0.0"
        rpsu_status = "OFF"
        rpsu_uptime = "0"
        voltage = "0"
        current = "0"
        leak_current = "0"

        try:
            # 1. Вход в меню модема
            _ = send_command(tn, "2", delay=1)

            # 2. Получаем температуру ДО входа в RPSU-меню!
            status_resp = send_command(tn, "STATUS", delay=1)
            cleaned_temp = clean_response(status_resp)
            temperature = extract_temperature(cleaned_temp)

            # 3. Переходим к платам
            _ = send_command(tn, "%1", delay=1)
            echo_resp = send_command(tn, "ECHO", delay=1)
            cleaned_echo = clean_response(echo_resp)

            if "04" not in cleaned_echo:
                status_var.set("Нет RPSU")
                tn.close()
                time.sleep(polling_interval * 60)
                continue

            # 4. Подключаемся к RPSU (плата 04)
            _ = send_command(tn, "%104", delay=1)
            _ = send_command(tn, "1", delay=1)
            show_resp = send_command(tn, "SHOW", delay=2)
            cleaned_show = clean_response(show_resp)

            # 5. Извлекаем RPSU-параметры
            rpsu_status = extract_rpsu_status(cleaned_show)
            rpsu_uptime = extract_uptime(cleaned_show)
            voltage = extract_value(cleaned_show, "Voltage")
            current = extract_value(cleaned_show, "Current")
            leak_current = extract_value(cleaned_show, "Leak Current")

            # 6. Обновляем GUI
            status_display = "Авария" if rpsu_status == "OFF" else rpsu_status
            status_var.set(status_display)
            uptime_var.set(rpsu_uptime)
            voltage_var.set(voltage)
            current_var.set(current)
            leak_current_var.set(leak_current)
            temperature_var.set(temperature)

            # --- ЦВЕТОВОЕ ИНДИКАТОРНОЕ УВЕДОМЛЕНИЕ (без окон!) ---
            try:
                temp_val = float(temperature)
            except ValueError:
                temp_val = 0.0

            def update_temp_color():
                try:
                    if temp_val > 40.0:
                        temperature_label.config(fg="orange")  # 🔶 только оранжевый (по ТЗ)
                    else:
                        temperature_label.config(fg="black")
                except Exception as e:
                    print(f"GUI color update failed: {e}")

            if window:
                window.after(0, update_temp_color)
            else:
                update_temp_color()

            # 7. Запись в CSV
            write_to_csv(name, rpsu_status, rpsu_uptime, voltage, current, leak_current, temperature)
            if utc_enabled and utc_enabled.get():
                write_to_utc_csv(name, rpsu_status, rpsu_uptime, voltage, current, leak_current, temperature)

        except Exception as e:
            print(f"[{name}] Error in loop: {e}")
            status_var.set("Ошибка")
        finally:
            try:
                tn.close()
            except:
                pass
            time.sleep(polling_interval * 60)


# --- GUI: вспомогательные функции ---
def show_debug_log(device_name):
    debug_window = tk.Toplevel()
    debug_window.title(f"Журнал — {device_name}")
    text = scrolledtext.ScrolledText(debug_window, width=90, height=30, font=("Courier", 9))
    text.pack(padx=10, pady=10)

    try:
        with open(f"{device_name}_data.csv", 'r', encoding='utf-8') as f:
            for line in f:
                text.insert(tk.END, line)
    except FileNotFoundError:
        text.insert(tk.END, "Файл журнала не найден.")
    text.config(state=tk.DISABLED)


def create_tray_icon(window_local):
    def create_image():
        img = Image.new("RGB", (64, 64), "white")
        dc = ImageDraw.Draw(img)
        dc.rectangle([16, 16, 48, 48], outline="black", fill="blue")
        return img

    def restore(icon, item):
        icon.stop()
        window_local.deiconify()

    def exit_app(icon, item):
        icon.stop()
        window_local.quit()
        sys.exit()

    icon = pystray.Icon(
        "RPSU Monitor",
        create_image(),
        "RPSU Monitor",
        pystray.Menu(
            pystray.MenuItem("Открыть", restore),
            pystray.MenuItem("Выход", exit_app)
        )
    )
    icon.run()


def add_device(ip_entry, name_entry, status_label):
    global devices
    ip = ip_entry.get().strip()
    name = name_entry.get().strip()
    if len(devices) >= MAX_DEVICES:
        status_label.config(text=f"Максимум {MAX_DEVICES} устройств!", fg="red")  # ✅ Исправлено!
        return
    if not ip or not name:
        status_label.config(text="IP и имя обязательны!", fg="red")
        return
    devices.append({"ip": ip, "name": name, "port": 23})
    save_devices_to_file()
    status_label.config(text=f"Добавлено: {name}", fg="green")
    ip_entry.delete(0, tk.END)
    name_entry.delete(0, tk.END)
    update_main_window()


def delete_device(name):
    global devices
    devices = [d for d in devices if d["name"] != name]
    save_devices_to_file()
    update_main_window()


def edit_device(old_name, new_ip, new_name):
    global devices
    for d in devices:
        if d["name"] == old_name:
            d["ip"] = new_ip
            d["name"] = new_name
            break
    save_devices_to_file()
    update_main_window()


def open_edit_window(device):
    win = tk.Toplevel()
    win.title(f"Редактировать: {device['name']}")
    tk.Label(win, text="IP:").grid(row=0, column=0, padx=5, pady=5)
    ip_e = tk.Entry(win)
    ip_e.insert(0, device["ip"])
    ip_e.grid(row=0, column=1, padx=5, pady=5)
    tk.Label(win, text="Имя:").grid(row=1, column=0, padx=5, pady=5)
    name_e = tk.Entry(win)
    name_e.insert(0, device["name"])
    name_e.grid(row=1, column=1, padx=5, pady=5)

    def save():
        ip, name = ip_e.get().strip(), name_e.get().strip()
        if ip and name:
            edit_device(device["name"], ip, name)
            win.destroy()
        else:
            messagebox.showerror("Ошибка", "Заполните оба поля!")
    tk.Button(win, text="Сохранить", command=save).grid(row=2, column=0, columnspan=2, pady=10)


def update_main_window():
    global main_frame, devices, window
    for w in main_frame.winfo_children():
        w.destroy()

    for i, device in enumerate(devices):
        frame = tk.Frame(main_frame, relief="groove", bd=2, padx=10, pady=10)
        frame.grid(row=0, column=i, padx=15, pady=15)

        # Заголовок с иконками
        hdr = tk.Frame(frame)
        hdr.grid(row=0, column=0, columnspan=2, sticky="ew")
        tk.Label(hdr, text=f"{device['name']} — {device['ip']}", font=("Arial", 12, "bold")).pack(side="left")
        tk.Label(hdr, text="⚙️", cursor="hand2", font=("Arial", 14)).pack(side="right", padx=5)
        tk.Label(hdr, text="❌", cursor="hand2", font=("Arial", 14)).pack(side="right", padx=5)

        # Привязка иконок
        for child in hdr.winfo_children():
            if child.cget("text") == "⚙️":
                child.bind("<Button-1>", lambda e, d=device: open_edit_window(d))
            elif child.cget("text") == "❌":
                child.bind("<Button-1>", lambda e, n=device["name"]: delete_device(n))

        # Переменные
        status_var = tk.StringVar()
        uptime_var = tk.StringVar()
        voltage_var = tk.StringVar()
        current_var = tk.StringVar()
        leak_var = tk.StringVar()
        temp_var = tk.StringVar()

        # Инициализация из CSV
        s, u, v, c, l, t = get_last_data_from_csv(device["name"])
        status_var.set("Авария" if s == "OFF" else s or "Нет данных")
        uptime_var.set(u or "—")
        voltage_var.set(v or "—")
        current_var.set(c or "—")
        leak_var.set(l or "—")
        temp_var.set(t or "0.0")

        # Поля
        fields = [
            ("Статус ДП:", status_var),
            ("В работе (ч):", uptime_var),
            ("Напряжение (В):", voltage_var),
            ("Ток (mA):", current_var),
            ("Ток утечки (mA):", leak_var),
            ("Температура (°C):", temp_var),
        ]
        temp_label = None
        for idx, (lbl, var) in enumerate(fields):
            tk.Label(frame, text=lbl, font=("Arial", 10)).grid(row=idx+1, column=0, sticky="w", pady=2)
            if lbl == "Температура (°C):":
                temp_label = tk.Label(frame, textvariable=var, font=("Arial", 10))
                temp_label.grid(row=idx+1, column=1, sticky="w", pady=2)
            else:
                tk.Label(frame, textvariable=var, font=("Arial", 10)).grid(row=idx+1, column=1, sticky="w", pady=2)

        tk.Button(frame, text="Журнал", command=lambda n=device["name"]: show_debug_log(n)).grid(
            row=len(fields)+1, column=0, columnspan=2, pady=10
        )

        if temp_label:
            threading.Thread(
                target=device_monitoring,
                args=(device, status_var, uptime_var, voltage_var, current_var, leak_var, temp_var, temp_label),
                daemon=True
            ).start()


# --- GUI: основное окно ---
def create_gui():
    global main_frame, utc_enabled, devices, window
    window = tk.Tk()
    window.title("RPSU Monitor v0.2")
    window.geometry("1000x500")

    utc_enabled = tk.BooleanVar(value=False)

    tabs = ttk.Notebook(window)
    main_tab = ttk.Frame(tabs)
    cfg_tab = ttk.Frame(tabs)
    help_tab = ttk.Frame(tabs)
    tabs.add(main_tab, text="Главная")
    tabs.add(cfg_tab, text="Параметры")
    tabs.add(help_tab, text="Справка")
    tabs.pack(expand=1, fill="both", padx=10, pady=10)

    # Главная вкладка
    main_frame = tk.Frame(main_tab)
    main_frame.pack(fill="both", expand=True, padx=10, pady=10)

    # Параметры
    tk.Label(cfg_tab, text="IP:").grid(row=0, column=0, sticky="e", padx=5, pady=5)
    ip_e = tk.Entry(cfg_tab, width=20)
    ip_e.grid(row=0, column=1, padx=5, pady=5)
    tk.Label(cfg_tab, text="Имя:").grid(row=1, column=0, sticky="e", padx=5, pady=5)
    name_e = tk.Entry(cfg_tab, width=20)
    name_e.grid(row=1, column=1, padx=5, pady=5)
    status_lbl = tk.Label(cfg_tab, text="")
    status_lbl.grid(row=2, column=0, columnspan=2)
    tk.Button(cfg_tab, text="➕ Добавить", command=lambda: add_device(ip_e, name_e, status_lbl)).grid(row=3, column=0, columnspan=2, pady=10)

    tk.Label(cfg_tab, text="Интервал (мин):").grid(row=4, column=0, sticky="e", padx=5, pady=5)
    combo = ttk.Combobox(cfg_tab, values=[1, 5, 10, 15, 30, 60], width=18, state="readonly")
    combo.set(polling_interval)
    combo.grid(row=4, column=1, padx=5, pady=5)

    def apply_interval():
        global polling_interval
        try:
            polling_interval = int(combo.get())
            messagebox.showinfo("Успех", f"Интервал: {polling_interval} мин")
        except:
            pass
    tk.Button(cfg_tab, text="Применить", command=apply_interval).grid(row=5, column=0, columnspan=2, pady=10)

    tk.Checkbutton(cfg_tab, text="UTC", variable=utc_enabled, font=("Arial", 10)).grid(row=6, column=0, columnspan=2, pady=10)

    # Справка
    help_txt = scrolledtext.ScrolledText(help_tab, width=90, height=20, font=("Arial", 9))
    help_txt.pack(padx=10, pady=10)
    help_txt.insert(tk.END,
"""ИНСТРУКЦИЯ
──────────────────────────────────────
1. Добавьте до 5 устройств (IP + имя).
2. Редактируйте (⚙️) или удаляйте (❌).
3. Температура >40°C → оранжевая метка.
4. Данные сохраняются в CSV (по имени).
5. Закрытие → подтверждение; сворачивание → в трей.
6. Настройки хранятся в devices.json.

Версия: 0.2 (2025)
by UterGrooll""")
    help_txt.config(state=tk.DISABLED)

    # === ИСПРАВЛЕННАЯ ЛОГИКА ЗАКРЫТИЯ / СВОРАЧИВАНИЯ ===
    window.is_minimized_to_tray = False

    def on_window_close():
        """Вызывается при нажатии на крестик (X) — полный выход"""
        answer = messagebox.askyesno(
            "Подтверждение выхода",
            "Завершить работу программы полностью?",
            parent=window
        )
        if answer:
            window.destroy()
            sys.exit()

    def on_window_minimize(event):
        """Сворачивание окна → в трей"""
        # event.widget == window, и state меняется до/после
        # Используем after_idle для проверки реального состояния
        window.after_idle(_check_minimize)

    def _check_minimize():
        if window.state() == 'iconic' and not window.is_minimized_to_tray:
            window.is_minimized_to_tray = True
            window.withdraw()
            # Запускаем трей в фоне
            threading.Thread(target=lambda: create_tray_icon(window), daemon=True).start()

    def on_window_restore(event):
        window.is_minimized_to_tray = False

    # Привязка событий
    window.protocol("WM_DELETE_WINDOW", on_window_close)   # ✅ крестик → подтверждение
    window.bind("<Unmap>", on_window_minimize)            # ✅ сворачивание → в трей
    window.bind("<Map>", on_window_restore)               # сброс флага

    # === конец новой логики ===

    devices = load_devices_from_file()
    update_main_window()
    window.mainloop()


if __name__ == "__main__":
    create_gui()