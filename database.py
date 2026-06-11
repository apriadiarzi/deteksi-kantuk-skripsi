import sqlite3

# Membuat tabel users dan history dalam satu fungsi
def create_db():
    conn = sqlite3.connect('user_data.db')
    cursor = conn.cursor()
    
    # Buat tabel users
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        email TEXT NOT NULL,
        password TEXT NOT NULL
    )
    ''')

    # Buat tabel history
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        start_time TEXT NOT NULL,
        stop_time TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    ''')

    # Buat tabel detail history
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS detail_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        start_time TEXT NOT NULL,
        stop_time TEXT NOT NULL,
        drowsiness_type TEXT,
        drowsiness_time TEXT,
        drowsiness_duration TEXT,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    ''')
    
    conn.commit()
    conn.close()

# Fungsi untuk mendapatkan user_id berdasarkan username
def get_user_id_by_username(username):
    conn = sqlite3.connect('user_data.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT id FROM users WHERE username=?', (username,))
    user = cursor.fetchone()
    
    conn.close()
    
    return user[0] if user else None

# Menambah histori baru
def add_history(username, start_time, stop_time):
    user_id = get_user_id_by_username(username)
    
    if user_id is None:
        print("User not found.")
        return False
    
    conn = sqlite3.connect('user_data.db')
    cursor = conn.cursor()
    
    cursor.execute('''
    INSERT INTO history (user_id, start_time, stop_time)
    VALUES (?, ?, ?)
    ''', (user_id, start_time, stop_time))
    
    conn.commit()
    conn.close()
    print("History added successfully.")
    return True

def add_history_detail(username, start_time, stop_time, drowsiness_data):
    user_id = get_user_id_by_username(username)
    
    if user_id is None:
        print("User not found.")
        return False
    
    conn = sqlite3.connect('user_data.db')
    cursor = conn.cursor()
    
    for data in drowsiness_data:
        cursor.execute('''
        INSERT INTO history_detail (user_id, start_time, stop_time, drowsiness_type, drowsiness_time, drowsiness_duration)
        VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, start_time, stop_time, data['jenis_mengantuk'], data['waktu_mengantuk'], data['lama_mengantuk']))
    
    conn.commit()
    conn.close()
    print("History added successfully.")
    return True

# Fungsi untuk menambah pengguna baru ke database
def add_user(username, email, password):
    conn = sqlite3.connect('user_data.db')
    cursor = conn.cursor()
    
    cursor.execute('''
    INSERT INTO users (username, email, password)
    VALUES (?, ?, ?)
    ''', (username, email, password))
    
    conn.commit()
    conn.close()

# Fungsi untuk mengecek apakah username sudah ada di database
def check_username_exists(username):
    conn = sqlite3.connect('user_data.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM users WHERE username=?', (username,))
    user = cursor.fetchone()
    
    conn.close()
    
    return user is not None

# Fungsi untuk mengecek login
def check_login(username, password):
    conn = sqlite3.connect('user_data.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM users WHERE username=? AND password=?', (username, password))
    user = cursor.fetchone()
    
    conn.close()
    
    return user is not None

# Fungsi mengambil data dari tabel history
def get_history_by_username(username):
    user_id = get_user_id_by_username(username)
    
    if user_id is None:
        return []
    
    conn = sqlite3.connect('user_data.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM history WHERE user_id=?', (user_id,))
    history_data = cursor.fetchall()
    
    conn.close()
    
    return history_data

def get_history_detail(username, start_time):
    user_id = get_user_id_by_username(username)
    
    if user_id is None:
        return []
    
    conn = sqlite3.connect('user_data.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM history_detail WHERE user_id=? AND start_time=?', (user_id, start_time))
    history_data_detail = cursor.fetchall()
    
    conn.close()
    
    return history_data_detail

# Fungsi untuk mendapatkan user_id berdasarkan username
def get_user_id_by_username(username):
    conn = sqlite3.connect('user_data.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT id FROM users WHERE username=?', (username,))
    user = cursor.fetchone()
    
    conn.close()
    
    return user[0] if user else None