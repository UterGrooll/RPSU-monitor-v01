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

# Глобальные переменные для хранения устройств и настроек
devices = []  # Список устройств: [{"ip": "192.168.1.5", "name": "Device 1", "port": 23}, ...]
MAX_DEVICES = 3  # Максимальное количество устройств
polling_interval = 60  # Периодичность опроса по умолчанию (в минутах)


# Функция для сохранения устройств в файл
def save_devices_to_file():
    with open("devices.json", "w") as file:
        json.dump(devices, file)


# Функция для загрузки устройств из файла
def load_devices_from_file():
    try:
        with open("devices.json", "r") as file:
            return json.load(file)
    except FileNotFoundError:
        return []


# Функция для подключения к устройству через Telnet
def connect_to_device(ip, port):
    try:
        tn = telnetlib.Telnet(ip, port, timeout=5)
        print(f"Connected to device {ip}:{port}")
        return tn
    except Exception as e:
        print(f"Connection failed: {e}")
        return None


# Функция для отправки команды и получения ответа с паузой
def send_command(tn, command, delay=1):
    try:
        tn.write(command.encode('ascii') + b"\r\n")
        time.sleep(delay)  # Задержка перед чтением ответа
        response = tn.read_very_eager().decode('ascii')
        return response
    except Exception as e:
        print(f"Failed to send command: {e}")
        return ""


# Функция для очистки ответа от управляющих символов
def clean_response(response):
    cleaned_response = re.sub(r'\x1B\[[0-9;]*[a-zA-Z]', '', response)
    cleaned_response = re.sub(r'[^\x20-\x7E]', '', cleaned_response)  # Оставляем только ASCII printable символы
    return cleaned_response.strip()


# Функция для извлечения параметров
def extract_value(response, key):
    match = re.search(f"{key}[:=]\s*([-\d.]+)", response)
    return match.group(1) if match else "0"


def extract_uptime(response):
    match = re.search(r"RPSU Uptime[:=]\s*(\d+) Hours", response)
    return match.group(1) if match else "0"


def extract_rpsu_status(response):
    match = re.search(r"RPSU Status[:=]\s*(ON|OFF)", response, re.IGNORECASE)
    return match.group(1).upper() if match else "OFF"


# Функция записи данных в CSV (основной файл)
def write_to_csv(device_name, rpsu_status, rpsu_uptime, voltage, current, leak_current):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    filename = f"{device_name}_data.csv"

    # Если файл не существует, записываем заголовок
    file_exists = os.path.exists(filename)

    with open(filename, mode="a", newline="", encoding="utf-8") as file:
        writer = csv.writer(file, delimiter=";")
        if not file_exists:
            writer.writerow(["Timestamp", "Status", "Uptime", "Voltage", "Current", "Leak Current"])
        writer.writerow([timestamp, rpsu_status, rpsu_uptime, voltage, current, leak_current])

    print(f"Data written to {filename}")


# Функция записи данных в CSV (UTC-файл)
def write_to_utc_csv(device_name, rpsu_status, rpsu_uptime, voltage, current, leak_current):
    # Переводим время в UTC (минус 3 часа)
    timestamp_utc = (datetime.now() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")

    # Преобразуем статус в числовой формат
    status_numeric = 1 if rpsu_status == "ON" else 0

    # Если статус "0", остальные значения тоже должны быть "0"
    if status_numeric == 0:
        rpsu_uptime, voltage, current, leak_current = 0, 0, 0, 0

    # Имя файла с суффиксом _utc_data.csv
    filename = f"{device_name}_utc_data.csv"

    # Если файл не существует, записываем заголовок
    file_exists = os.path.exists(filename)

    with open(filename, mode="a", newline="", encoding="utf-8") as file:
        writer = csv.writer(file, delimiter=";")
        if not file_exists:
            writer.writerow(["Timestamp", "Status", "Uptime", "Voltage", "Current", "Leak Current"])
        writer.writerow([timestamp_utc, status_numeric, rpsu_uptime, voltage, current, leak_current])

    print(f"UTC data written to {filename}")


# Функция для получения последних данных из CSV
def get_last_data_from_csv(device_name):
    try:
        filename = f"{device_name}_data.csv"
        if not os.path.exists(filename):
            return "", "", "", "", ""  # Файл не существует, возвращаем пустые значения

        with open(filename, mode="r", encoding="utf-8") as file:
            reader = list(csv.reader(file, delimiter=";"))
            if len(reader) > 1:  # Если есть данные (кроме заголовка)
                last_row = reader[-1]  # Получаем последнюю строку
                return last_row[1], last_row[2], last_row[3], last_row[4], last_row[5]  # RPSU Status, Uptime, Voltage, Current, Leak Current
            else:
                return "", "", "", "", ""  # Файл существует, но данных нет
    except Exception as e:
        print(f"Error reading CSV file: {e}")
        return "", "", "", "", ""


# Функция работы с устройством
def device_monitoring(device, status_var, uptime_var, voltage_var, current_var, leak_current_var):
    global polling_interval
    while True:
        tn = connect_to_device(device["ip"], device["port"])
        if not tn:
            status_var.set("Нет связи")
            time.sleep(polling_interval * 60)  # Используем глобальную переменную polling_interval
            continue

        try:
            # Переход в нужное меню
            response = send_command(tn, "2", delay=2)  # Вход в раздел
            response = send_command(tn, "%1", delay=2)  # Выбор подменю
            response = send_command(tn, "ECHO", delay=2)  # Проверка ответа
            cleaned_response = clean_response(response)

            if "04" not in cleaned_response:
                status_var.set("Нет связи")
                tn.close()
                time.sleep(polling_interval * 60)
                continue

            response = send_command(tn, "%104", delay=2)
            response = send_command(tn, "1", delay=2)  # Переход в режим SHOW
            response = send_command(tn, "SHOW", delay=3)
            cleaned_response = clean_response(response)

            # Извлечение данных
            rpsu_status = extract_rpsu_status(cleaned_response)
            rpsu_uptime = extract_uptime(cleaned_response)
            voltage = extract_value(cleaned_response, "Voltage")
            current = extract_value(cleaned_response, "Current")
            leak_current = extract_value(cleaned_response, "Leak Current")

            # Обновление данных в GUI
            if rpsu_status == "OFF":
                status_var.set("Авария")
            else:
                status_var.set(rpsu_status)
            uptime_var.set(rpsu_uptime)
            voltage_var.set(voltage)
            current_var.set(current)
            leak_current_var.set(leak_current)

            # Запись в CSV
            write_to_csv(device["name"], rpsu_status, rpsu_uptime, voltage, current, leak_current)

            # Если галочка "UTC" активна, записываем данные в UTC-формат
            if utc_enabled.get():
                write_to_utc_csv(device["name"], rpsu_status, rpsu_uptime, voltage, current, leak_current)

        except Exception as e:
            print(f"An error occurred: {e}")
            status_var.set("Нет связи")
        finally:
            tn.close()
            time.sleep(polling_interval * 60)  # Используем глобальную переменную polling_interval


# Функция для отображения отладочной информации
def show_debug_log(device_name):
    debug_window = tk.Toplevel()
    debug_window.title(f"Журнал данных - {device_name}")

    # Текстовое поле для отображения отладочной информации
    debug_text = scrolledtext.ScrolledText(debug_window, width=80, height=20)
    debug_text.pack(padx=10, pady=10)

    # Чтение данных из CSV и вывод в текстовое поле
    try:
        filename = f"{device_name}_data.csv"
        with open(filename, mode="r") as file:
            reader = csv.reader(file, delimiter=";")
            for row in reader:
                debug_text.insert(tk.END, "; ".join(row) + "\n")
    except FileNotFoundError:
        debug_text.insert(tk.END, "Журнал данных: No data found in CSV file.\n")

    debug_text.config(state=tk.DISABLED)  # Запрет редактирования текста


# Функция для создания иконки в трее
def create_tray_icon(window):
    # Создаем изображение для иконки
    def create_image():
        image = Image.new('RGB', (64, 64), color='white')
        dc = ImageDraw.Draw(image)
        dc.rectangle([16, 16, 48, 48], outline='black', fill='blue')
        return image

    # Функция для восстановления окна
    def restore_window(icon, item):
        icon.stop()
        window.deiconify()

    # Функция для выхода из приложения
    def exit_app(icon, item):
        icon.stop()
        window.destroy()
        sys.exit()

    # Создаем меню для иконки в трее
    menu = pystray.Menu(
        pystray.MenuItem("Открыть", restore_window),
        pystray.MenuItem("Выход", exit_app)
    )

    # Создаем иконку в трее
    icon = pystray.Icon("RPSU Monitor", create_image(), "RPSU Monitor", menu)

    # Запускаем иконку в трее
    icon.run()


# Функция для добавления устройства
def add_device(ip_entry, name_entry, status_label):
    global devices
    if len(devices) >= MAX_DEVICES:
        status_label.config(text="Достигнуто максимальное количество устройств!")
        return

    ip = ip_entry.get()
    name = name_entry.get()

    if not ip or not name:
        status_label.config(text="Поля IP и имя не могут быть пустыми!")
        return

    devices.append({"ip": ip, "name": name, "port": 23})
    save_devices_to_file()  # Сохраняем устройства в файл
    status_label.config(text=f"Устройство {name} добавлено!")
    ip_entry.delete(0, tk.END)
    name_entry.delete(0, tk.END)

    # Обновляем главное окно
    update_main_window()


# Функция для удаления устройства
def delete_device(device_name):
    global devices
    devices = [device for device in devices if device["name"] != device_name]
    save_devices_to_file()  # Сохраняем изменения
    update_main_window()  # Обновляем интерфейс


# Функция для редактирования устройства
def edit_device(device_name, new_ip, new_name):
    global devices
    for device in devices:
        if device["name"] == device_name:
            device["ip"] = new_ip
            device["name"] = new_name
            break
    save_devices_to_file()  # Сохраняем изменения
    update_main_window()  # Обновляем интерфейс


# Функция для открытия окна редактирования
def open_edit_window(device):
    edit_window = tk.Toplevel()
    edit_window.title(f"Редактирование {device['name']}")

    tk.Label(edit_window, text="Новый IP-адрес:").grid(row=0, column=0, padx=10, pady=10)
    new_ip_entry = tk.Entry(edit_window)
    new_ip_entry.insert(0, device["ip"])
    new_ip_entry.grid(row=0, column=1, padx=10, pady=10)

    tk.Label(edit_window, text="Новое имя:").grid(row=1, column=0, padx=10, pady=10)
    new_name_entry = tk.Entry(edit_window)
    new_name_entry.insert(0, device["name"])
    new_name_entry.grid(row=1, column=1, padx=10, pady=10)

    def save_changes():
        new_ip = new_ip_entry.get()
        new_name = new_name_entry.get()
        if new_ip and new_name:
            edit_device(device["name"], new_ip, new_name)
            edit_window.destroy()
        else:
            messagebox.showerror("Ошибка", "Поля IP и имя не могут быть пустыми!")

    tk.Button(edit_window, text="Сохранить", command=save_changes).grid(row=2, column=0, columnspan=2, padx=10, pady=10)


# Функция для обновления главного окна
def update_main_window():
    for widget in main_frame.winfo_children():
        widget.destroy()

    for i, device in enumerate(devices):
        # Создаем фрейм для каждого устройства
        device_frame = tk.Frame(main_frame, borderwidth=2, relief="groove")
        device_frame.grid(row=0, column=i, padx=20, pady=20)  # Увеличили отступы

        # Переменные для отображения данных
        status_var = tk.StringVar()
        uptime_var = tk.StringVar()
        voltage_var = tk.StringVar()
        current_var = tk.StringVar()
        leak_current_var = tk.StringVar()

        # Инициализация данных из CSV (если файл существует)
        status, uptime, voltage, current, leak_current = get_last_data_from_csv(device["name"])
        if status == "OFF":
            status = "Авария"
        elif not status:
            status = "Нет связи"

        status_var.set(status)
        uptime_var.set(uptime)
        voltage_var.set(voltage)
        current_var.set(current)
        leak_current_var.set(leak_current)

        # Заголовок с именем устройства и IP
        header_frame = tk.Frame(device_frame)
        header_frame.grid(row=0, column=0, columnspan=2, sticky="ew")

        # Название устройства и IP (увеличили шрифт)
        tk.Label(
            header_frame,
            text=f"{device['name']} - {device['ip']}",
            font=("Arial", 14, "bold")  # Увеличили шрифт
        ).grid(row=0, column=0, sticky="w")

        # Иконка "Редактировать" (шестерёнка)
        edit_icon = tk.Label(
            header_frame,
            text="⚙️",
            font=("Arial", 16),  # Увеличили шрифт
            cursor="hand2"
        )
        edit_icon.grid(row=0, column=1, sticky="e", padx=5)
        edit_icon.bind("<Button-1>", lambda e, dev=device: open_edit_window(dev))

        # Иконка "Удалить" (крестик)
        delete_icon = tk.Label(
            header_frame,
            text="❌",
            font=("Arial", 16),  # Увеличили шрифт
            cursor="hand2"
        )
        delete_icon.grid(row=0, column=2, sticky="e", padx=5)
        delete_icon.bind("<Button-1>", lambda e, name=device["name"]: delete_device(name))

        # Метки для отображения текущих данных (увеличили шрифт и отступы)
        tk.Label(device_frame, text="Статус ДП:", font=("Arial", 12)).grid(row=1, column=0, sticky="w", pady=5)
        tk.Label(device_frame, textvariable=status_var, font=("Arial", 12)).grid(row=1, column=1, sticky="w", pady=5)

        tk.Label(device_frame, text="В работе (часы):", font=("Arial", 12)).grid(row=2, column=0, sticky="w", pady=5)
        tk.Label(device_frame, textvariable=uptime_var, font=("Arial", 12)).grid(row=2, column=1, sticky="w", pady=5)

        tk.Label(device_frame, text="Напряжение (В):", font=("Arial", 12)).grid(row=3, column=0, sticky="w", pady=5)
        tk.Label(device_frame, textvariable=voltage_var, font=("Arial", 12)).grid(row=3, column=1, sticky="w", pady=5)

        tk.Label(device_frame, text="Ток (mA):", font=("Arial", 12)).grid(row=4, column=0, sticky="w", pady=5)
        tk.Label(device_frame, textvariable=current_var, font=("Arial", 12)).grid(row=4, column=1, sticky="w", pady=5)

        tk.Label(device_frame, text="Ток утечки (mA):", font=("Arial", 12)).grid(row=5, column=0, sticky="w", pady=5)
        tk.Label(device_frame, textvariable=leak_current_var, font=("Arial", 12)).grid(row=5, column=1, sticky="w", pady=5)

        # Кнопка для отображения журнала данных (увеличили шрифт)
        tk.Button(
            device_frame,
            text="Журнал данных",
            font=("Arial", 12),  # Увеличили шрифт
            command=lambda name=device["name"]: show_debug_log(name)
        ).grid(row=6, column=0, columnspan=2, pady=10)

        # Запуск мониторинга в отдельном потоке
        threading.Thread(
            target=device_monitoring,
            args=(device, status_var, uptime_var, voltage_var, current_var, leak_current_var),
            daemon=True
        ).start()


# Графический интерфейс
def create_gui():
    global main_frame, utc_enabled

    window = tk.Tk()
    window.title("RPSU Monitor")

    # Инициализация переменной для галочки "UTC"
    utc_enabled = tk.BooleanVar(value=False)

    # Вкладки
    tab_control = ttk.Notebook(window)
    main_tab = ttk.Frame(tab_control)
    settings_tab = ttk.Frame(tab_control)
    help_tab = ttk.Frame(tab_control)  # Вкладка "Справка"
    tab_control.add(main_tab, text="Главная")
    tab_control.add(settings_tab, text="Параметры")
    tab_control.add(help_tab, text="Справка")
    tab_control.pack(expand=1, fill="both")

    # Главная вкладка
    main_frame = tk.Frame(main_tab)
    main_frame.pack(padx=10, pady=10)

    # Вкладка "Параметры"
    ip_label = tk.Label(settings_tab, text="IP-адрес:")
    ip_label.grid(row=0, column=0, padx=10, pady=10)
    ip_entry = tk.Entry(settings_tab)
    ip_entry.grid(row=0, column=1, padx=10, pady=10)

    name_label = tk.Label(settings_tab, text="Имя устройства:")
    name_label.grid(row=1, column=0, padx=10, pady=10)
    name_entry = tk.Entry(settings_tab)
    name_entry.grid(row=1, column=1, padx=10, pady=10)

    status_label = tk.Label(settings_tab, text="")
    status_label.grid(row=2, column=0, columnspan=2, padx=10, pady=10)

    add_button = tk.Button(settings_tab, text="+ Добавить устройство",
                           command=lambda: add_device(ip_entry, name_entry, status_label))
    add_button.grid(row=3, column=0, columnspan=2, padx=10, pady=10)

    # Выбор периодичности опроса
    polling_label = tk.Label(settings_tab, text="Периодичность опроса (минут):")
    polling_label.grid(row=4, column=0, padx=10, pady=10)

    polling_options = [1, 5, 10, 15, 30, 60]
    polling_combobox = ttk.Combobox(settings_tab, values=polling_options, state="readonly")
    polling_combobox.set(polling_interval)  # Устанавливаем значение по умолчанию
    polling_combobox.grid(row=4, column=1, padx=10, pady=10)

    # Кнопка для обновления периодичности опроса
    def apply_polling_interval():
        global polling_interval
        selected_interval = int(polling_combobox.get())
        polling_interval = selected_interval
        messagebox.showinfo("Успех", f"Периодичность опроса изменена на {selected_interval} минут")

    apply_button = tk.Button(settings_tab, text="Применить", command=apply_polling_interval)
    apply_button.grid(row=5, column=0, columnspan=2, padx=10, pady=10)

    # Галочка "UTC"
    utc_checkbox = tk.Checkbutton(
        settings_tab,
        text="UTC ✅",
        variable=utc_enabled,
        font=("Arial", 12)
    )
    utc_checkbox.grid(row=6, column=0, columnspan=2, padx=10, pady=10)

    # Вкладка "Справка"
    help_text = scrolledtext.ScrolledText(help_tab, width=80, height=20)
    help_text.pack(padx=10, pady=10)

    # Текст справки
    help_content = """
              **Инструкция:**  
    1. Добавление устройства: Введите IP и имя→"+ Добавить"(до 3 устройств).  
    2. Редактирование: "⚙️" → новый IP/имя →"Сохранить".  
    3. Удаление: "❌" рядом с устройством.  
    4. Журнал данных:  — история опросов из CSV-файла.  
    5. Периодичность: "Параметры"→ выбрать интервал→ "Применить".
    6. UTC - дополнительно сохранение файла для СVS-считывателя Scada  
    6. Сворачивание: Закрытие — в трей, восстановление — из трея, выход —"Выход".  
    7. Настройки сохраняются в JSON. 
        **Версия 0.1, 2025.**  
        by UterGrooll.
        """

    help_text.insert(tk.END, help_content)
    help_text.config(state=tk.DISABLED)  # Запрет редактирования текста

    # Сворачивание в трей при закрытии окна
    def on_closing():
        window.withdraw()  # Скрываем окно
        create_tray_icon(window)  # Создаем иконку в трее

    window.protocol("WM_DELETE_WINDOW", on_closing)

    # Загружаем устройства при запуске
    global devices
    devices = load_devices_from_file()
    update_main_window()  # Обновляем главное окно

    window.mainloop()


if __name__ == "__main__":
    create_gui()