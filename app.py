from collections import defaultdict
from datetime import datetime, timedelta
import os
import sqlite3
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, session, redirect, url_for
import razorpay
from dotenv import load_dotenv

load_dotenv()

password_Admin = os.getenv('password')

app = Flask(__name__)
app.secret_key = 'supersecretkey'
app.config['UPLOAD_FOLDER'] = 'static/uploads'

def get_db_connection():
    conn = sqlite3.connect('menu.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()

    conn.execute('''
    CREATE TABLE IF NOT EXISTS menu_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        price REAL NOT NULL,
        category TEXT,
        image TEXT
    )
    ''')

    conn.execute('''
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        order_type TEXT NOT NULL,  -- now used for table_number
        address TEXT,
        items TEXT,
        total REAL,
        status TEXT DEFAULT 'pending',
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    conn.execute('''
    CREATE TABLE IF NOT EXISTS report (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        price REAL NOT NULL,
        date TEXT DEFAULT (DATE('now'))
    )
    ''')


    conn.commit()
    conn.close()

@app.route('/admin', methods=['GET', 'POST'])
def admin():
    conn = get_db_connection()
    if request.method == 'POST':
        if 'add' in request.form:
            name = request.form['name']
            price = float(request.form['price'])
            category = request.form['category']

            image_file = request.files['image']
            image_filename = ""
            if image_file and image_file.filename:
                image_filename = secure_filename(image_file.filename)
                image_path = os.path.join(app.config['UPLOAD_FOLDER'], image_filename)
                image_file.save(image_path)

            conn.execute("""
                INSERT INTO menu_items (name, price, category, image)
                VALUES (?, ?, ?, ?)""",
                (name, price, category, image_filename))
            conn.commit()

        elif 'delete' in request.form:
            item_id = request.form['delete']
            conn.execute("DELETE FROM menu_items WHERE id = ?", (item_id,))
            conn.commit()

    items = conn.execute("SELECT * FROM menu_items").fetchall()
    conn.close()
    return render_template('admin.html', items=items)

@app.route('/', methods=['GET', 'POST'])
def customer_menu():
    conn = get_db_connection()
    items = conn.execute("SELECT * FROM menu_items ORDER BY category").fetchall()
    conn.close()

    if 'cart' not in session:
        session['cart'] = {}

    if request.method == 'POST':
        item_id = request.form['item_id']
        qty = int(request.form['qty'])
        if qty > 0:
            session['cart'][item_id] = session['cart'].get(item_id, 0) + qty
        session.modified = True
    return render_template('menu.html', items=items, cart=session['cart'])

@app.route('/checkout', methods=['GET', 'POST'])
def checkout():
    if 'update_item' in request.form:
        item_id = request.form['update_item']
        action = request.form['action']

        if item_id in session.get('cart', {}):
            if action == 'plus':
                session['cart'][item_id] += 1
            elif action == 'minus':
                if session['cart'][item_id] > 1:
                    session['cart'][item_id] -= 1
                else:
                    session['cart'].pop(item_id)
            elif action == 'remove':
                session['cart'].pop(item_id, None)

            session.modified = True
        return redirect(url_for('checkout'))

    cart = session.get('cart', {})
    conn = get_db_connection()
    items, total = [], 0
    for item_id, qty in cart.items():
        row = conn.execute("SELECT * FROM menu_items WHERE id = ?", (item_id,)).fetchone()
        if row:
            row = dict(row)
            row['qty'] = qty
            row['subtotal'] = row['price'] * qty
            items.append(row)
            total += row['subtotal']
    conn.close()

    if request.method == 'POST':
        name = request.form.get('customer_name', '').strip()
        table_number = request.form.get('table_number', '').strip()

        if not name or not table_number:
            return "Name and table number are required", 400

        item_list = [f"{item['name']} x {item['qty']}" for item in items]
        item_string = "; ".join(item_list)

        conn = get_db_connection()
        conn.execute("""
            INSERT INTO orders (name, order_type, address, items, total, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
        """, (name, f"Table {table_number}", 'N/A', item_string, total))
        conn.commit()
        conn.close()

        session.pop('cart', None)
        return redirect(url_for('order_status', customer=name))


    return render_template('checkout.html', items=items, total=total)

@app.route('/status/<customer>')
def order_status(customer):
    conn = get_db_connection()
    order = conn.execute("""
        SELECT * FROM orders WHERE name = ? ORDER BY timestamp DESC LIMIT 1
    """, (customer,)).fetchone()

    parsed_items = []
    if order:
        for item_string in order['items'].split(';'):
            item_string = item_string.strip()
            if ' x ' in item_string:
                name, qty = item_string.split(' x ')
                menu_item = conn.execute("SELECT * FROM menu_items WHERE name = ?", (name.strip(),)).fetchone()
                if menu_item:
                    price = float(menu_item['price'])
                    subtotal = price * int(qty)
                    parsed_items.append({
                        'name': name.strip(),
                        'qty': int(qty),
                        'price': price,
                        'subtotal': subtotal
                    })

    conn.close()

    if not order:
        return "Order not found"

    return render_template('status.html', order=order, parsed_items=parsed_items)


@app.route('/admin/orders')
def admin_orders():
    filter_status = request.args.get('filter')
    conn = get_db_connection()
    if filter_status in ['pending', 'accepted', 'rejected', 'prepared', 'preparing']:
        orders = conn.execute(
            "SELECT * FROM orders WHERE status = ? ORDER BY timestamp DESC",
            (filter_status,)
        ).fetchall()
    else:
        orders = conn.execute("SELECT * FROM orders ORDER BY timestamp DESC").fetchall()
    conn.close()
    return render_template('admin_orders.html', orders=orders, current_filter=filter_status)


@app.route('/admin/update/<int:order_id>', methods=['POST'])
def update_order(order_id):
    action = request.form['action']

    if action == 'accept' or action == 'prepare':
        new_status = 'preparing'
    elif action == 'prepared':
        new_status = 'prepared'
    elif action == 'reject':
        new_status = 'rejected'
    else:
        return redirect(url_for('admin_orders'))

    conn = get_db_connection()
    conn.execute("UPDATE orders SET status = ? WHERE id = ?", (new_status, order_id))
    conn.commit()
    conn.close()

    return redirect(url_for('admin_orders'))


@app.route('/admin_login', methods=['POST'])
def admin_login():
    username = request.form['username']
    password = request.form['password']

    # Simple login
    if username == 'admin' and password == password_Admin:
        session['admin'] = True
        return redirect(url_for('admin'))
    else:
        return "Invalid credentials", 401


@app.route('/place_order', methods=['POST'])
def place_order():
    name = request.form.get('customer_name')
    table_number = request.form.get('table_number')

    if not all([name, table_number]):
        return "Missing required fields", 400

    cart = session.get('cart', {})
    conn = get_db_connection()
    items, total = [], 0

    for item_id, qty in cart.items():
        row = conn.execute("SELECT * FROM menu_items WHERE id = ?", (item_id,)).fetchone()
        if row:
            row = dict(row)
            row['qty'] = qty
            row['subtotal'] = row['price'] * qty
            items.append(row)
            total += row['subtotal']

    item_string = "; ".join(f"{item['name']} x {item['qty']}" for item in items)

    conn.execute("""
        INSERT INTO orders (name, order_type, address, items, total, status)
        VALUES (?, ?, ?, ?, ?, 'pending')
    """, (name, table_number, f'Table {table_number}', item_string, total))

    conn.execute("INSERT INTO report (price) VALUES (?)", (total,))

    conn.commit()
    conn.close()

    session.pop('cart', None)
    return redirect(url_for('order_status', customer=name))


@app.route('/admin/sales_report')
def sales_report():
    filter_type = request.args.get('filter', 'all')
    conn = get_db_connection()

    if filter_type == 'daily':
        query = "SELECT date, SUM(price) as total FROM report GROUP BY date ORDER BY date DESC"

    elif filter_type == 'weekly':
        query = """
        SELECT 
            strftime('%Y', date) as year,
            strftime('%W', date) as week,
            strftime('%m', date) as month,
            SUM(price) as total
        FROM report
        GROUP BY year, week
        ORDER BY year DESC, week DESC
        """

    elif filter_type == 'monthly':
        query = """
        SELECT 
            strftime('%Y', date) as year,
            strftime('%m', date) as month,
            SUM(price) as total
        FROM report
        GROUP BY year, month
        ORDER BY year DESC, month DESC
        """

    elif filter_type == 'yearly':
        query = """
        SELECT 
            strftime('%Y', date) as year,
            SUM(price) as total
        FROM report
        GROUP BY year
        ORDER BY year DESC
        """
    else:
        query = "SELECT date, SUM(price) as total FROM report GROUP BY date ORDER BY date DESC"

    rows = conn.execute(query).fetchall()
    conn.close()

    # Format results for the template
    formatted_data = []

    if filter_type == 'daily' or filter_type == 'all':
        for row in rows:
            raw_date = datetime.strptime(row['date'], '%Y-%m-%d')
            formatted_date = raw_date.strftime('%d-%m-%y')
            formatted_data.append({
                'label': formatted_date,
                'total': row['total']
            })


    elif filter_type == 'weekly':
        for row in rows:
            month_name = datetime.strptime(row['month'], '%m').strftime('%B')
            week_number = int(row['week']) + 1  # SQLite week starts from 0
            label = f"Week {week_number} - {month_name} {row['year']}"
            formatted_data.append({'label': label, 'total': row['total']})

    elif filter_type == 'monthly':
        for row in rows:
            month_name = datetime.strptime(row['month'], '%m').strftime('%B')
            label = f"{month_name} {row['year']}"
            formatted_data.append({'label': label, 'total': row['total']})

    elif filter_type == 'yearly':
        for row in rows:
            label = f"{row['year']}"
            formatted_data.append({'label': label, 'total': row['total']})

    total_sales = sum(row['total'] for row in formatted_data)

    return render_template('sales_report.html', data=formatted_data, total_sales=total_sales, filter_type=filter_type)




if __name__ == '__main__':
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    init_db()
    app.run(debug=True)
