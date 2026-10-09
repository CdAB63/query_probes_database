#!/usr/bin/env python3
"""Gráficos de CO₂ para sondas Estufa — Python 3.10+.

Instalação Ubuntu/Kubuntu:
  sudo apt install python3-tk python3-venv
  python3 -m venv .venv
  .venv/bin/pip install 'psycopg[binary]>=3.1,<4' 'matplotlib>=3.7,<4'
  .venv/bin/python co2_estufas.py

Informe a conexão e clique em Carregar sondas. Marque por clique as sondas
(em seleção múltipla, sem Ctrl), informe início/fim e gere os gráficos.
A barra de ferramentas de cada gráfico permite zoom e salvar PNG/PDF/SVG.

Esquema esperado: public.sensor(id, sensor_name),
public.sensor_reading(sensor_id, co2_ppm, reading_time).
Datas: DD/MM/AAAA HH:MM:SS, interpretadas no fuso indicado.
O início é inclusivo e o fim exclusivo. reading_time deve ser timestamptz;
se for timestamp sem fuso, os valores são interpretados no fuso indicado.
Não grava no banco. Senha apenas em memória. Consultas em segundo plano.
Limite de 500 mil pontos por consulta: reduza o período caso seja excedido.
"""
import math
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

try:
    import psycopg
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
    import matplotlib.dates as mdates
except ImportError as exc:
    raise SystemExit("Instale as dependências: pip install 'psycopg[binary]' matplotlib\n" + str(exc))

MAX_POINTS = 500_000


def parse_period(start, end, timezone):
    zone = ZoneInfo(timezone)
    first = datetime.strptime(start.strip(), '%d/%m/%Y %H:%M:%S').replace(tzinfo=zone)
    last = datetime.strptime(end.strip(), '%d/%m/%Y %H:%M:%S').replace(tzinfo=zone)
    if first >= last:
        raise ValueError('O início deve ser anterior ao fim.')
    return first, last, zone


def connect(params, timezone):
    conn = psycopg.connect(**params, connect_timeout=15)
    conn.execute('SET TRANSACTION READ ONLY')
    conn.execute("SELECT set_config('TimeZone', %s, true)", (timezone,))
    conn.execute("SELECT set_config('statement_timeout', '120000', true)")
    return conn


def load_sensors(params, timezone):
    with connect(params, timezone) as conn:
        return conn.execute(
            "SELECT id, sensor_name FROM public.sensor "
            "WHERE sensor_name LIKE %s ORDER BY sensor_name, id", ('Estufa%',)
        ).fetchall()


def load_readings(params, timezone, sensors, first, last):
    data = {sid: (name, [], []) for sid, name in sensors}
    total = skipped = 0
    with connect(params, timezone) as conn:
        with conn.cursor(name='co2_stream') as cur:
            cur.itersize = 10000
            cur.execute(
                'SELECT s.id, r.reading_time, r.co2_ppm '
                'FROM public.sensor s JOIN public.sensor_reading r ON s.id = r.sensor_id '
                'WHERE s.id = ANY(%s) AND r.reading_time >= %s AND r.reading_time < %s '
                'AND r.co2_ppm IS NOT NULL ORDER BY r.reading_time',
                ([sid for sid, _ in sensors], first, last)
            )
            zone = ZoneInfo(timezone)
            for sid, timestamp, value in cur:
                total += 1
                if total > MAX_POINTS:
                    raise ValueError('Mais de 500 mil pontos. Reduza o período ou o número de sondas.')
                value = float(value)
                if timestamp is None or not math.isfinite(value):
                    skipped += 1
                    continue
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=zone)
                data[sid][1].append(timestamp.astimezone(zone))
                data[sid][2].append(value)
    return data, skipped


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('Estufas — gráficos de CO₂')
        self.geometry('850x680')
        self.minsize(730, 620)
        self.events = queue.Queue()
        self.sensors = []
        self.busy = False
        self.fields = {}
        conn = ttk.LabelFrame(self, text='Conexão PostgreSQL', padding=12)
        conn.pack(fill='x', padx=12, pady=12)
        for i, (key, label, default) in enumerate([
            ('host', 'Servidor', 'localhost'), ('port', 'Porta', '5432'),
            ('dbname', 'Banco', 'painel_de_dados_db'), ('user', 'Usuário', 'painel-de-dados'),
            ('password', 'Senha', ''), ('timezone', 'Fuso horário', 'America/Sao_Paulo')
        ]):
            row, col = divmod(i, 2)
            ttk.Label(conn, text=label).grid(row=row, column=col*2, sticky='w', padx=5, pady=5)
            var = tk.StringVar(value=default)
            self.fields[key] = var
            ttk.Entry(conn, textvariable=var, show='*' if key == 'password' else '', width=26).grid(
                row=row, column=col*2+1, sticky='ew', padx=5, pady=5)
        conn.columnconfigure(1, weight=1)
        conn.columnconfigure(3, weight=1)
        self.load_button = ttk.Button(conn, text='Carregar sondas', command=self.fetch_sensors)
        self.load_button.grid(row=3, column=0, columnspan=4, pady=8)
        box = ttk.LabelFrame(self, text='Sondas com nome iniciado em Estufa — clique para marcar/desmarcar', padding=10)
        box.pack(fill='both', expand=True, padx=12)
        scroll = ttk.Scrollbar(box)
        scroll.pack(side='right', fill='y')
        self.listbox = tk.Listbox(box, selectmode=tk.MULTIPLE, exportselection=False, height=10,
                                  yscrollcommand=scroll.set)
        self.listbox.pack(fill='both', expand=True)
        scroll.configure(command=self.listbox.yview)
        choices = ttk.Frame(self)
        choices.pack(fill='x', padx=12, pady=6)
        ttk.Button(choices, text='Selecionar todas', command=lambda: self.listbox.select_set(0, tk.END)).pack(side='left')
        ttk.Button(choices, text='Limpar seleção', command=lambda: self.listbox.select_clear(0, tk.END)).pack(side='left', padx=8)
        period = ttk.LabelFrame(self, text='Período — DD/MM/AAAA HH:MM:SS (fim exclusivo)', padding=10)
        period.pack(fill='x', padx=12, pady=6)
        now = datetime.now(ZoneInfo('America/Sao_Paulo')).replace(microsecond=0)
        self.start = tk.StringVar(value=(now-timedelta(days=2)).strftime('%d/%m/%Y %H:%M:%S'))
        self.end = tk.StringVar(value=now.strftime('%d/%m/%Y %H:%M:%S'))
        for col, (label, var) in enumerate([('Início', self.start), ('Fim', self.end)]):
            ttk.Label(period, text=label).grid(row=0, column=col*2, padx=5)
            ttk.Entry(period, textvariable=var, width=25).grid(row=0, column=col*2+1, padx=5)
        buttons = ttk.Frame(self)
        buttons.pack(pady=8)
        self.plot_buttons = []
        for text, mode in [('Gráfico superposto', 'overlay'), ('Gráficos isolados', 'individual')]:
            button = ttk.Button(buttons, text=text, command=lambda m=mode: self.fetch_plot(m))
            button.pack(side='left', padx=8)
            self.plot_buttons.append(button)
        self.status = tk.StringVar(value='Configure a conexão e carregue as sondas.')
        ttk.Label(self, textvariable=self.status, wraplength=800).pack(fill='x', padx=16, pady=8)
        self.after(100, self.poll)

    def settings(self):
        params = {key: var.get() for key, var in self.fields.items() if key != 'timezone'}
        params['port'] = int(params['port'])
        if not 1 <= params['port'] <= 65535:
            raise ValueError('Porta inválida.')
        timezone = self.fields['timezone'].get().strip()
        ZoneInfo(timezone)
        return params, timezone

    def run_job(self, work, callback):
        if self.busy:
            return
        self.busy = True
        for button in [self.load_button, *self.plot_buttons]:
            button.configure(state='disabled')
        self.status.set('Consultando o banco…')
        def worker():
            try:
                self.events.put((callback, work(), None))
            except Exception as exc:
                self.events.put((callback, None, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            callback, result, error = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            for button in [self.load_button, *self.plot_buttons]:
                button.configure(state='normal')
            if error:
                self.status.set('Consulta não concluída.')
                messagebox.showerror('Erro', error, parent=self)
            else:
                try:
                    callback(result)
                except Exception as exc:
                    self.status.set('Falha ao exibir os dados.')
                    messagebox.showerror('Erro', str(exc), parent=self)
        self.after(100, self.poll)

    def fetch_sensors(self):
        try:
            params, timezone = self.settings()
        except Exception as exc:
            messagebox.showerror('Configuração inválida', str(exc), parent=self)
            return
        def show(rows):
            self.sensors = rows
            self.listbox.delete(0, tk.END)
            for sid, name in rows:
                self.listbox.insert(tk.END, f'{name}  [ID: {sid}]')
            self.status.set(f'{len(rows)} sondas encontradas. Selecione as desejadas.')
        self.run_job(lambda: load_sensors(params, timezone), show)

    def fetch_plot(self, mode):
        selected = [self.sensors[i] for i in self.listbox.curselection()]
        if not selected:
            messagebox.showinfo('Seleção', 'Selecione ao menos uma sonda.', parent=self)
            return
        try:
            params, timezone = self.settings()
            first, last, zone = parse_period(self.start.get(), self.end.get(), timezone)
        except Exception as exc:
            messagebox.showerror('Dados inválidos', str(exc), parent=self)
            return
        self.run_job(lambda: load_readings(params, timezone, selected, first, last),
                     lambda result: self.show_plots(result, mode, zone, first, last))

    def show_plots(self, result, mode, zone, first, last):
        data, skipped = result
        populated = [(sid, record) for sid, record in data.items() if record[1]]
        empty = [f'{record[0]} [ID: {sid}]' for sid, record in data.items() if not record[1]]
        points = sum(len(record[1]) for _, record in populated)
        self.status.set(f'{points:,} pontos; {len(populated)} sondas com dados; {skipped} valores não finitos ignorados.')
        if empty:
            messagebox.showinfo('Sem dados no período', '\n'.join(empty), parent=self)
        if not populated:
            return
        window = tk.Toplevel(self)
        window.title('CO₂ — ' + ('superpostos' if mode == 'overlay' else 'isolados'))
        window.geometry('1050x740')
        if mode == 'overlay':
            self.plot_frame(window, populated, zone, first, last)
        else:
            notebook = ttk.Notebook(window)
            notebook.pack(fill='both', expand=True)
            for sid, record in populated:
                frame = ttk.Frame(notebook)
                notebook.add(frame, text=f'{record[0]} [ID: {sid}]')
                self.plot_frame(frame, [(sid, record)], zone, first, last)

    def plot_frame(self, parent, series, zone, first, last):
        fig = Figure(figsize=(10, 6), dpi=100, layout='constrained')
        ax = fig.add_subplot(111)
        for sid, (name, dates, values) in series:
            ax.plot(dates, values, linewidth=1, label=f'{name} [ID: {sid}]')
        ax.set(title='Concentração de CO₂', xlabel=f'Data e hora — {zone.key}', ylabel='CO₂ (ppm)')
        ax.set_xlim(first, last)
        ax.grid(True, alpha=0.3)
        ax.legend(loc='best', fontsize=9)
        locator = mdates.AutoDateLocator(tz=zone)
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator, tz=zone))
        canvas = FigureCanvasTkAgg(fig, master=parent)
        toolbar = NavigationToolbar2Tk(canvas, parent, pack_toolbar=False)
        toolbar.update()
        toolbar.pack(side='bottom', fill='x')
        canvas.get_tk_widget().pack(fill='both', expand=True)
        canvas.draw()
        parent._plot_refs = (fig, canvas, toolbar)


if __name__ == '__main__':
    App().mainloop()
