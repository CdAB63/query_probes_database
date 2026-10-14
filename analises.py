#!/usr/bin/env python3
"""Gráficos de CO₂, temperatura e umidade para sondas Estufa — Python 3.10+.

Instalação Ubuntu/Kubuntu:
  sudo apt install python3-tk python3-venv
  python3 -m venv .venv
  .venv/bin/pip install 'psycopg[binary]>=3.1,<4' 'matplotlib>=3.7,<4'
  .venv/bin/python co2_estufas.py

Informe a conexão e clique em Carregar sondas. Marque por clique as sondas
(em seleção múltipla, sem Ctrl), informe início/fim e gere os gráficos.
A barra de ferramentas de cada gráfico permite zoom e salvar PNG/PDF/SVG.

Esquema esperado: public.sensor(id, sensor_name),
public.sensor_reading(sensor_id, co2_ppm, temperature_c, humidity_percent, reading_time).
Datas: DD/MM/AAAA HH:MM:SS, interpretadas no fuso indicado.
O início é inclusivo e o fim exclusivo. reading_time deve ser timestamptz;
se for timestamp sem fuso, os valores são interpretados no fuso indicado.
Não grava no banco. Senha apenas em memória. Consultas em segundo plano.
Limite de 500 mil pontos por consulta: reduza o período caso seja excedido.

ANÁLISES (os gráficos originais continuam usando as leituras brutas):
- Selecione as sondas e o período como antes. Use Comparar pares ou Tendências.
- Escolha CO₂, Temperatura ou Umidade no seletor. Usuário padrão: grafana.
- Unidades: ppm, °C e %; diferenças de umidade em pontos percentuais (p.p.).
- Janela: média do parâmetro selecionado em intervalos fixos ancorados no início solicitado.
  Não há interpolação, deslocamento temporal ou preenchimento de lacunas.
- A → B significa diferença B-A. São geradas todas as combinações, uma vez.
  O offset é vertical, na unidade de diferença do parâmetro, e subtraído de B. Opções: média de B-A
  (minimiza RMSE após ajuste), primeira diferença comum, ou sem ajuste.
  A média remove a diferença média no período; não é calibração da sonda.
- RMSE e MAE medem a diferença residual na unidade do parâmetro (menores = mais próximas).
  Pearson r mede associação linear; séries constantes têm r indefinido.
- Tendência: regressão linear sobre médias por janela, com peso igual para
  cada janela válida, expressa em unidades/h e unidades/dia. Pelo menos 3 janelas.
  A tolerância em unidades/h define quando a inclinação é considerada pequena.
  R² é descritivo; não há teste de significância. A inclinação do parâmetro não
  comprova drift instrumental: também pode refletir mudança real no ambiente.
  A inclinação da diferença entre sondas mede divergência relativa.
- Correlação móvel: selecione exatamente duas sondas e use o novo botão.
  Duração padrão 15 min, avanço 1 min, base de alinhamento 60 s.
  Médias na mesma célula formam pares; exige por padrão 5 pares e 80% de
  cobertura. Usa somente janelas completas [início, fim), com resultado
  no centro temporal. Duração e avanço são múltiplos da base.
  Pearson constante/sem variação suficiente é indefinido, nunca zero.
  RMSE complementar remove o offset médio local, mas preserva diferenças
  de amplitude. Nenhuma correção de atraso temporal é aplicada.
  Janelas sobrepostas não são observações independentes; análise descritiva.
- Resumos e séries analisadas podem ser exportados em CSV (UTF-8, ';').
"""
import math
import csv
from itertools import combinations
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

try:
    import psycopg
    import numpy as np
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
    import matplotlib.dates as mdates
except ImportError as exc:
    raise SystemExit("Instale as dependências: pip install 'psycopg[binary]' matplotlib\n" + str(exc))

MAX_POINTS = 500_000
MAX_ANALYSIS_CELLS = 2_000_000
OFFSET_MODES = ('Média das diferenças', 'Primeiro ponto comum', 'Sem ajuste')


PARAMETERS = {
    'co2_ppm': dict(label='CO₂', unit='ppm', delta_unit='ppm'),
    'temperature_c': dict(label='Temperatura', unit='°C', delta_unit='°C'),
    'humidity_percent': dict(label='Umidade', unit='%', delta_unit='p.p.'),
}


def parameter_info(parameter):
    # Whitelist: nenhum nome de coluna fornecido livremente entra na consulta SQL.
    if parameter not in PARAMETERS:
        raise ValueError('Parâmetro inválido.')
    return PARAMETERS[parameter]


def linear_trend(seconds, values, tolerance=0):
    """Regressão descritiva por mínimos quadrados, sem inferência estatística."""
    mask = np.isfinite(values)
    x = np.asarray(seconds, dtype=float)[mask] / 3600
    y = np.asarray(values, dtype=float)[mask]
    if len(y) < 3 or np.ptp(x) == 0:
        return None
    xc, yc = x - x.mean(), y - y.mean()
    slope = float(np.dot(xc, yc) / np.dot(xc, xc))
    intercept = float(y.mean() - slope*x.mean())
    fitted = intercept + slope*x
    total = float(np.dot(yc, yc))
    r2 = None if total == 0 else float(1 - np.sum((y-fitted)**2)/total)
    direction = 'positivo' if slope > tolerance else 'negativo' if slope < -tolerance else 'dentro da tolerância'
    return dict(slope=slope, intercept=intercept, r2=r2, direction=direction,
                change=float(slope*np.ptp(x)), hours=float(np.ptp(x)), n=len(y))


def analyze(data, first, last, step=60, offset_mode=OFFSET_MODES[0], tolerance=0, pairs=True, parameter='co2_ppm'):
    """Média por janela; diferenças B-A apenas em janelas com dados de ambas.

    Não interpola nem preenche lacunas. A grade começa no início solicitado.
    Janelas vazias permanecem NaN, inclusive nos gráficos. Tendências usam
    todas as janelas válidas da respectiva sonda; pares usam apenas as comuns.
    """
    parameter_info(parameter)
    if not math.isfinite(step) or step <= 0:
        raise ValueError('A janela deve ser um número positivo de segundos.')
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError('A tolerância deve ser um número não negativo na unidade do parâmetro por hora.')
    if offset_mode not in OFFSET_MODES:
        raise ValueError('Modo de offset inválido.')
    duration = last.timestamp() - first.timestamp()
    if duration <= 0:
        raise ValueError('O início deve ser anterior ao fim.')
    size = math.ceil(duration/step)
    count = len(data)
    pair_count = count*(count-1)//2 if pairs else 0
    if size*(count + 3*pair_count) > MAX_ANALYSIS_CELLS:
        raise ValueError('Grade de análise muito grande. Aumente a janela, reduza o período ou selecione menos sondas.')
    # Centros das janelas, inclusive da última janela parcial.
    seconds = (np.arange(size)*step + np.minimum((np.arange(size)+1)*step, duration))/2
    dates = [datetime.fromtimestamp(first.timestamp()+float(s), first.tzinfo) for s in seconds]
    series = {}
    for sid, (name, timestamps, values) in data.items():
        elapsed = np.array([t.timestamp()-first.timestamp() for t in timestamps])
        vals = np.asarray(values, dtype=float)
        valid = (elapsed >= 0) & (elapsed < duration) & np.isfinite(vals)
        indices = np.floor(elapsed[valid]/step).astype(int)
        counts = np.bincount(indices, minlength=size)
        sums = np.bincount(indices, weights=vals[valid], minlength=size)
        means = np.full(size, np.nan)
        np.divide(sums, counts, out=means, where=counts > 0)
        series[sid] = dict(name=name, values=means, trend=linear_trend(seconds, means, tolerance),
                           n=int(np.count_nonzero(counts)), raw_n=int(valid.sum()))
    comparisons = []
    if pairs:
        for a, b in combinations(series, 2):
            av, bv = series[a]['values'], series[b]['values']
            mask = np.isfinite(av) & np.isfinite(bv)
            n = int(mask.sum())
            item = dict(a=a, b=b, label=f"{series[a]['name']} [{a}] → {series[b]['name']} [{b}]", n=n)
            if n < 3:
                item['error'] = 'Menos de 3 janelas comuns com dados.'
                comparisons.append(item)
                continue
            raw = bv[mask]-av[mask]
            offset = float(raw.mean()) if offset_mode == OFFSET_MODES[0] else float(raw[0]) if offset_mode == OFFSET_MODES[1] else 0.0
            delta = np.full(size, np.nan)
            delta[mask] = raw-offset
            residual = delta[mask]
            correlation = None
            if np.ptp(av[mask]) > 0 and np.ptp(bv[mask]) > 0:
                correlation = float(np.corrcoef(av[mask], bv[mask])[0, 1])
            item.update(offset=offset, delta=delta, correlation=correlation,
                        rmse=float(np.sqrt(np.mean(residual**2))),
                        mae=float(np.mean(np.abs(residual))),
                        raw_mean=float(raw.mean()),
                        trend=linear_trend(seconds, delta, tolerance))
            comparisons.append(item)
    return dict(series=series, pairs=comparisons, dates=dates, seconds=seconds,
                size=size, step=step, offset_mode=offset_mode, tolerance=tolerance, parameter=parameter)


def rolling_settings(first, last, base, window, advance, min_points, coverage, min_std):
    """Durações em segundos. Janelas completas, alinhadas à grade comum."""
    if any(not math.isfinite(x) or x <= 0 for x in (base, window, advance)):
        raise ValueError('Base, duração e avanço devem ser positivos e finitos.')
    if not math.isfinite(coverage) or not 0 <= coverage <= 100:
        raise ValueError('Cobertura deve estar entre 0 e 100%.')
    if not math.isfinite(min_std) or min_std < 0:
        raise ValueError('Desvio mínimo deve ser não negativo e finito.')
    if not isinstance(min_points, int) or min_points < 3:
        raise ValueError('Use ao menos 3 pontos comuns por janela.')
    ratios = (window/base, advance/base)
    if any(not math.isfinite(x) or x > MAX_ANALYSIS_CELLS for x in ratios):
        raise ValueError('Duração ou avanço muito grande em relação à base.')
    if any(not math.isclose(x, round(x), rel_tol=0, abs_tol=1e-8) for x in ratios):
        raise ValueError('Duração e avanço devem ser múltiplos da base de alinhamento.')
    width, hop = (int(round(x)) for x in ratios)
    if width < min_points or hop < 1:
        raise ValueError('Diminua a base ou aumente a duração: faltam pontos por janela.')
    duration = last.timestamp()-first.timestamp()
    if duration < window:
        raise ValueError('O período selecionado deve conter ao menos uma janela completa.')
    count = math.floor((duration-window)/advance + 1e-10)+1
    if count > 100_000 or count*width > 50_000_000:
        raise ValueError('Análise muito grande. Aumente o avanço/base ou reduza o período.')
    return width, hop, count


def rolling_correlation(data, first, last, parameter='co2_ppm', base=60,
                        window=900, advance=60, min_points=5, coverage=80, min_std=0):
    """Pearson local e RMSE após remover média de B-A; não preenche lacunas.

    Cada ponto é média de uma célula da grade. Só pares válidos na mesma
    célula entram nas métricas. O centro corresponde à janela solicitada,
    mesmo quando existem lacunas internas. Limites [início, fim).
    """
    if len(data) != 2:
        raise ValueError('Selecione exatamente duas sondas para a correlação móvel.')
    width, hop, count = rolling_settings(first, last, base, window, advance,
                                       min_points, coverage, min_std)
    aligned = analyze(data, first, last, base, pairs=False, parameter=parameter)
    ids = list(aligned['series'])
    aa, bb = (aligned['series'][sid]['values'] for sid in ids)
    records = []
    for index in range(count):
        lo = index*hop
        av, bv = aa[lo:lo+width], bb[lo:lo+width]
        valid = np.isfinite(av) & np.isfinite(bv)
        x, y = av[valid], bv[valid]
        n = len(x)
        covered = 100*n/width
        start = first.timestamp()+index*advance
        record = dict(start=datetime.fromtimestamp(start, first.tzinfo),
                      end=datetime.fromtimestamp(start+window, first.tzinfo),
                      center=datetime.fromtimestamp(start+window/2, first.tzinfo),
                      n=n, coverage=covered, r=float('nan'), rmse=float('nan'),
                      offset=float('nan'), std_a=float('nan'), std_b=float('nan'))
        if n < min_points or covered+1e-10 < coverage:
            record['status'] = 'Dados insuficientes'
        else:
            xc, yc = x-x.mean(), y-y.mean()
            sx, sy = float(np.sqrt(np.mean(xc**2))), float(np.sqrt(np.mean(yc**2)))
            diff = y-x
            offset = float(diff.mean())
            record.update(offset=offset, rmse=float(np.sqrt(np.mean((diff-offset)**2))),
                          std_a=sx, std_b=sy)
            # Além do limiar escolhido, detecta variações na escala do erro numérico.
            eps = np.finfo(float).eps*32
            threshold_a = max(min_std, eps*float(np.max(np.abs(x))))
            threshold_b = max(min_std, eps*float(np.max(np.abs(y))))
            if sx <= threshold_a or sy <= threshold_b:
                record['status'] = 'Variação insuficiente para Pearson'
            else:
                record['r'] = float(np.clip(np.mean((xc/sx)*(yc/sy)), -1, 1))
                record['status'] = 'OK'
        records.append(record)
    return dict(parameter=parameter, aligned=aligned, ids=ids, records=records,
                first=first, last=last, base=base, window=window, advance=advance,
                min_points=min_points, coverage=coverage, min_std=min_std)


def rolling_figure(result, zone):
    info = parameter_info(result['parameter'])
    aligned, records = result['aligned'], result['records']
    dates = [r['center'] for r in records]
    fig = Figure(figsize=(11, 8), dpi=100, layout='constrained')
    axes = fig.subplots(4, 1, sharex=True, gridspec_kw={'height_ratios': [1.4, 1.3, 1, .65]})
    for sid in result['ids']:
        item = aligned['series'][sid]
        axes[0].plot(aligned['dates'], item['values'], label=f"{item['name']} [{sid}]", linewidth=1)
    axes[0].set_title(f"{info['label']} — correlação móvel: {result['window']/60:g} min, avanço {result['advance']/60:g} min")
    axes[0].legend(fontsize=8)
    axes[1].plot(dates, [r['r'] for r in records], '.-', markersize=3, label='Pearson r')
    axes[1].axhline(0, color='gray', linewidth=.7)
    axes[1].set_ylim(-1.05, 1.05)
    axes[2].plot(dates, [r['rmse'] for r in records], '.-', markersize=3, color='tab:orange')
    axes[3].plot(dates, [r['coverage'] for r in records], linewidth=1, color='tab:green')
    axes[3].axhline(result['coverage'], color='gray', linestyle='--', linewidth=.8)
    axes[3].set_ylim(-2, 102)
    labels = [f"{info['label']} ({info['unit']})", 'Pearson r',
              f"RMSE sem offset\n({info['delta_unit']})", 'Cobertura (%)']
    for ax, label in zip(axes, labels):
        time_axis(ax, zone, result['first'], result['last'], label)
        ax.tick_params(axis='both', labelsize=8)
        ax.yaxis.label.set_size(9)
        ax.set_xlabel('')
    axes[-1].set_xlabel(f'Data e hora — centro da janela — {zone.key}')
    return fig


def write_rolling_csv(path, result):
    info = parameter_info(result['parameter'])
    with open(path, 'w', encoding='utf-8-sig', newline='') as output:
        writer = csv.writer(output, delimiter=';')
        writer.writerow(['Parametro', result['parameter'], 'Unidade', info['unit'], 'Unidade_diferenca', info['delta_unit']])
        writer.writerow(['Inicio', result['first'].isoformat(), 'Fim_exclusivo', result['last'].isoformat()])
        writer.writerow(['Sonda_A', result['ids'][0], result['aligned']['series'][result['ids'][0]]['name'],
                         'Sonda_B', result['ids'][1], result['aligned']['series'][result['ids'][1]]['name']])
        writer.writerow(['Base_s', result['base'], 'Janela_s', result['window'], 'Avanco_s', result['advance'],
                         'Min_pontos', result['min_points'], 'Cobertura_min_pct', result['coverage'], 'Desvio_min', result['min_std']])
        writer.writerow(['Inicio_janela', 'Fim_exclusivo_janela', 'Centro_janela', 'N_pares', 'Cobertura_pct',
                         'Pearson_r', f"RMSE_sem_offset_{info['delta_unit']}", f"Offset_medio_B-A_{info['delta_unit']}",
                         f"Desvio_A_{info['delta_unit']}", f"Desvio_B_{info['delta_unit']}", 'Status'])
        for row in result['records']:
            metrics = [row[key] for key in ('r', 'rmse', 'offset', 'std_a', 'std_b')]
            writer.writerow([row['start'].isoformat(), row['end'].isoformat(), row['center'].isoformat(), row['n'], row['coverage']]
                            + [value if np.isfinite(value) else '' for value in metrics] + [row['status']])


def fmt(value):
    return '—' if value is None else f'{value:.4g}'


def time_axis(ax, zone, first, last, ylabel):
    ax.set(xlabel=f'Data e hora — {zone.key}', ylabel=ylabel, xlim=(first, last))
    ax.grid(True, alpha=0.3)
    locator = mdates.AutoDateLocator(tz=zone)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator, tz=zone))


def analysis_figure(result, key, mode, zone, first, last):
    """Figura independente de Tk: também permite verificação sem tela."""
    fig = Figure(figsize=(10, 6), dpi=100, layout='constrained')
    info = parameter_info(result['parameter'])
    unit, du = info['unit'], info['delta_unit']
    dates, seconds = result['dates'], result['seconds']
    if mode == 'similarity':
        item = result['pairs'][key]
        upper, lower = fig.subplots(2, 1, sharex=True)
        a, b = item['a'], item['b']
        mask = np.isfinite(item['delta'])
        upper.plot(dates, np.where(mask, result['series'][a]['values'], np.nan), label='A: '+result['series'][a]['name'])
        upper.plot(dates, np.where(mask, result['series'][b]['values']-item['offset'], np.nan), label='B ajustada: '+result['series'][b]['name'])
        upper.set_title(item['label'] + f"\nB ajustada = B − ({item['offset']:.4g} {du})")
        upper.legend(fontsize=9)
        time_axis(upper, zone, first, last, f"{info['label']} ({unit})")
        upper.set_xlabel('')
        lower.plot(dates, item['delta'], label='(B − offset) − A', linewidth=1)
        lower.axhline(0, color='gray', linewidth=0.8)
        trend = item['trend']
        lower.plot(dates, np.where(mask, trend['intercept']+trend['slope']*seconds/3600, np.nan),
                   '--', label=f"Tendência da diferença: {trend['slope']:+.4g} {du}/h")
        lower.set_title(f"RMSE={item['rmse']:.4g} {du} | MAE={item['mae']:.4g} {du} | r={fmt(item['correlation'])} | n={item['n']}")
        lower.legend(fontsize=9)
        time_axis(lower, zone, first, last, f'Diferença ({du})')
    else:
        item = result['series'][key]
        ax = fig.add_subplot(111)
        ax.plot(dates, item['values'], label='Média por janela', linewidth=1)
        trend = item['trend']
        fitted = trend['intercept']+trend['slope']*seconds/3600
        ax.plot(dates, np.where(np.isfinite(item['values']), fitted, np.nan), '--',
                label=f"Tendência: {trend['slope']:+.4g} {du}/h ({trend['direction']})")
        ax.set_title(f"{item['name']} [{key}]\nR²={fmt(trend['r2'])} | variação ajustada={trend['change']:+.4g} {du} | n={trend['n']}")
        ax.legend(fontsize=9)
        time_axis(ax, zone, first, last, f"{info['label']} ({unit})")
    return fig


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


def load_readings(params, timezone, sensors, first, last, parameter='co2_ppm'):
    parameter_info(parameter)
    data = {sid: (name, [], []) for sid, name in sensors}
    total = skipped = 0
    with connect(params, timezone) as conn:
        with conn.cursor(name='sensor_stream') as cur:
            cur.itersize = 10000
            cur.execute(
                f'SELECT s.id, r.reading_time, r.{parameter} '
                'FROM public.sensor s JOIN public.sensor_reading r ON s.id = r.sensor_id '
                'WHERE s.id = ANY(%s) AND r.reading_time >= %s AND r.reading_time < %s '
                f'AND r.{parameter} IS NOT NULL ORDER BY r.reading_time',
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
        self.title('Estufas — CO₂, temperatura e umidade')
        self.geometry('920x850')
        self.minsize(850, 790)
        self.events = queue.Queue()
        self.sensors = []
        self.busy = False
        self.fields = {}
        conn = ttk.LabelFrame(self, text='Conexão PostgreSQL', padding=12)
        conn.pack(fill='x', padx=12, pady=12)
        for i, (key, label, default) in enumerate([
            ('host', 'Servidor', 'localhost'), ('port', 'Porta', '5432'),
            ('dbname', 'Banco', 'painel_de_dados_db'), ('user', 'Usuário', 'grafana'),
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
        self.parameter = tk.StringVar(value='CO₂')
        ttk.Label(choices, text='Parâmetro:').pack(side='left', padx=(20, 5))
        self.parameter_selector = ttk.Combobox(choices, textvariable=self.parameter,
            values=[info['label'] for info in PARAMETERS.values()], state='readonly', width=16)
        self.parameter_selector.pack(side='left')
        self.parameter_selector.bind('<<ComboboxSelected>>', self.parameter_changed)
        period = ttk.LabelFrame(self, text='Período — DD/MM/AAAA HH:MM:SS (fim exclusivo)', padding=10)
        period.pack(fill='x', padx=12, pady=6)
        now = datetime.now(ZoneInfo('America/Sao_Paulo')).replace(microsecond=0)
        self.start = tk.StringVar(value=(now-timedelta(days=2)).strftime('%d/%m/%Y %H:%M:%S'))
        self.end = tk.StringVar(value=now.strftime('%d/%m/%Y %H:%M:%S'))
        for col, (label, var) in enumerate([('Início', self.start), ('Fim', self.end)]):
            ttk.Label(period, text=label).grid(row=0, column=col*2, padx=5)
            ttk.Entry(period, textvariable=var, width=25).grid(row=0, column=col*2+1, padx=5)
        options = ttk.LabelFrame(self, text='Opções para similaridade e tendência', padding=8)
        options.pack(fill='x', padx=12, pady=6)
        self.step = tk.StringVar(value='60')
        self.offset_mode = tk.StringVar(value=OFFSET_MODES[0])
        self.tolerance = tk.StringVar(value='0')
        ttk.Label(options, text='Janela (segundos)').grid(row=0, column=0, padx=5)
        ttk.Entry(options, textvariable=self.step, width=10).grid(row=0, column=1, padx=5)
        ttk.Label(options, text='Offset vertical').grid(row=0, column=2, padx=5)
        ttk.Combobox(options, textvariable=self.offset_mode, values=OFFSET_MODES,
                     state='readonly', width=24).grid(row=0, column=3, padx=5)
        self.tolerance_label = ttk.Label(options, text='Tolerância (ppm/h)')
        self.tolerance_label.grid(row=1, column=0, padx=5, pady=6)
        ttk.Entry(options, textvariable=self.tolerance, width=10).grid(row=1, column=1, padx=5)
        ttk.Label(options, text='Médias por janela; sem preencher lacunas.').grid(row=1, column=2, columnspan=2)
        ttk.Button(options, text='Como interpretar', command=self.show_help).grid(row=0, column=4, rowspan=2, padx=8)
        buttons = ttk.Frame(self)
        buttons.pack(pady=8)
        self.plot_buttons = []
        for i, (text, mode) in enumerate([('Gráfico superposto', 'overlay'), ('Gráficos isolados', 'individual'),
                                         ('Comparar pares', 'similarity'), ('Tendências', 'trend'),
                                         ('Correlação móvel', 'rolling')]):
            button = ttk.Button(buttons, text=text, command=lambda m=mode: self.rolling_dialog() if m == 'rolling' else self.fetch_plot(m))
            button.grid(row=i//2, column=i%2, padx=8, pady=4, sticky='ew')
            self.plot_buttons.append(button)
        self.status = tk.StringVar(value='Configure a conexão e carregue as sondas.')
        ttk.Label(self, textvariable=self.status, wraplength=800).pack(fill='x', padx=16, pady=8)
        self.after(100, self.poll)

    def selected_parameter(self):
        return next(key for key, info in PARAMETERS.items() if info['label'] == self.parameter.get())

    def parameter_changed(self, event=None):
        info = parameter_info(self.selected_parameter())
        self.tolerance_label.configure(text=f"Tolerância ({info['delta_unit']}/h)")
        # Uma tolerância numérica em ppm/h não se aplica automaticamente a °C/h.
        self.tolerance.set('0')

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
        if mode == 'similarity' and len(selected) < 2:
            messagebox.showinfo('Seleção', 'Selecione ao menos duas sondas para comparar.', parent=self)
            return
        try:
            params, timezone = self.settings()
            first, last, zone = parse_period(self.start.get(), self.end.get(), timezone)
            parameter = self.selected_parameter()
            if mode in ('similarity', 'trend'):
                step = float(self.step.get().replace(',', '.'))
                tolerance = float(self.tolerance.get().replace(',', '.'))
                offset_mode = self.offset_mode.get()
                if not math.isfinite(step) or step <= 0 or not math.isfinite(tolerance) or tolerance < 0:
                    raise ValueError('Janela deve ser positiva e tolerância não negativa; ambos devem ser finitos.')
        except Exception as exc:
            messagebox.showerror('Dados inválidos', str(exc), parent=self)
            return
        if mode in ('similarity', 'trend'):
            def work():
                data, skipped = load_readings(params, timezone, selected, first, last, parameter)
                return analyze(data, first, last, step, offset_mode, tolerance, pairs=mode == 'similarity', parameter=parameter), skipped
            self.run_job(work, lambda result: self.show_analysis(result, mode, zone, first, last))
        else:
            self.run_job(lambda: load_readings(params, timezone, selected, first, last, parameter),
                         lambda result: self.show_plots(result, mode, zone, first, last, parameter))

    def rolling_dialog(self):
        selected = [self.sensors[i] for i in self.listbox.curselection()]
        if len(selected) != 2:
            messagebox.showinfo('Seleção', 'Selecione exatamente duas sondas para a correlação móvel.', parent=self)
            return
        try:
            params, timezone = self.settings()
            first, last, zone = parse_period(self.start.get(), self.end.get(), timezone)
            parameter = self.selected_parameter()
        except Exception as exc:
            messagebox.showerror('Dados inválidos', str(exc), parent=self)
            return
        info = parameter_info(parameter)
        dialog = tk.Toplevel(self)
        dialog.title('Configurar correlação móvel')
        dialog.transient(self)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=15)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text=f"{selected[0][1]} × {selected[1][1]} — {info['label']}").grid(row=0, column=0, columnspan=2, sticky='w', pady=6)
        values = {}
        fields = [('window', 'Duração da janela (min)', '15'),
                  ('advance', 'Avanço (min)', '1'),
                  ('base', 'Base de alinhamento (segundos)', '60'),
                  ('min_points', 'Mínimo de pontos comuns', '5'),
                  ('coverage', 'Cobertura mínima (%)', '80'),
                  ('min_std', f"Desvio padrão mínimo ({info['delta_unit']})", '0')]
        for row, (key, label, default) in enumerate(fields, 1):
            values[key] = tk.StringVar(value=default)
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', padx=4, pady=4)
            ttk.Entry(frame, textvariable=values[key], width=12).grid(row=row, column=1, padx=6, pady=4)
        ttk.Label(frame, wraplength=550, text=(
            'A base define médias em uma grade comum, sem preencher lacunas. Duração e avanço '
            'devem ser múltiplos da base. Só janelas completas entram na análise.\n\n'
            'Pearson: +1 = variação conjunta, 0 = pouca associação linear, −1 = variação oposta. '
            'Séries constantes ou com poucos dados ficam sem correlação. O desvio mínimo permite '
            'ignorar trechos quase planos dominados por ruído.\n\n'
            'O gráfico inclui RMSE após retirar o offset médio de cada janela e a cobertura de dados. '
            'Pearson não mede topologia: mudanças de forma, amplitude e atrasos podem afetar as métricas. '
            'Os controles de offset/tolerância da tela principal não se aplicam a esta operação.'
        )).grid(row=7, column=0, columnspan=2, pady=10)
        def execute():
            try:
                settings = {key: float(var.get().replace(',', '.')) for key, var in values.items() if key != 'min_points'}
                settings['min_points'] = int(values['min_points'].get())
                settings['window'] *= 60
                settings['advance'] *= 60
                rolling_settings(first, last, **settings)
            except Exception as exc:
                messagebox.showerror('Configuração inválida', str(exc), parent=dialog)
                return
            dialog.destroy()
            def work():
                data, skipped = load_readings(params, timezone, selected, first, last, parameter)
                return rolling_correlation(data, first, last, parameter, **settings), skipped
            self.run_job(work, lambda payload: self.show_rolling(payload, zone))
        ttk.Button(frame, text='Calcular e plotar', command=execute).grid(row=8, column=0, pady=5)
        ttk.Button(frame, text='Cancelar', command=dialog.destroy).grid(row=8, column=1, pady=5)

    def show_rolling(self, payload, zone):
        result, skipped = payload
        records = result['records']
        valid = sum(np.isfinite(r['r']) for r in records)
        window = tk.Toplevel(self)
        window.title(parameter_info(result['parameter'])['label'] + ' — correlação móvel')
        window.geometry('1150x900')
        note = (f"{valid}/{len(records)} janelas com Pearson válido | base={result['base']:g} s | "
                f"mínimo={result['min_points']} pontos e {result['coverage']:g}% de cobertura\n"
                'Lacunas = dados ou variação insuficientes; não equivalem a r=0. '
                'Pearson ignora offset e escala; RMSE sem offset complementa a comparação de amplitude.')
        ttk.Label(window, text=note, wraplength=1100, padding=8).pack(fill='x')
        ttk.Button(window, text='Exportar correlação móvel CSV',
                   command=lambda: self.export_rolling(result)).pack(pady=4)
        graph = ttk.Frame(window)
        graph.pack(fill='both', expand=True)
        self.embed_figure(graph, rolling_figure(result, zone))
        self.status.set(f'Correlação móvel: {valid}/{len(records)} janelas válidas; {skipped} valores não finitos ignorados.')

    def export_rolling(self, result):
        path = filedialog.asksaveasfilename(parent=self, defaultextension='.csv',
            initialfile=f"correlacao_movel_{result['parameter']}.csv", filetypes=[('CSV', '*.csv')])
        if not path:
            return
        try:
            write_rolling_csv(path, result)
            messagebox.showinfo('Exportação', 'Correlação móvel salva. Valores indefinidos ficam vazios.', parent=self)
        except OSError as exc:
            messagebox.showerror('Exportação', str(exc), parent=self)

    def show_help(self):
        messagebox.showinfo('Como interpretar',
            'A → B: diferença (B − offset) − A. Offset é vertical, não temporal.\n\n'
            'Média das diferenças: subtrai de B a média de B−A nas janelas comuns. '
            'Primeiro ponto comum: usa apenas a primeira janela comum (mais sensível a ruído). '
            'Sem ajuste: preserva a diferença original.\n\n'
            'RMSE/MAE menores indicam curvas mais próximas. Pearson r próximo de +1 indica '
            'variação conjunta, mas não garante igualdade de valores.\n\n'
            'Unidades: CO₂ em ppm; temperatura em °C; umidade em %. Diferenças de umidade '
            'são pontos percentuais (p.p.): de 60% para 65% = +5 p.p.\n\n'
            'Inclinação positiva = subida; negativa = descida. A tolerância, na unidade de diferença por hora, '
            'define uma faixa em torno de zero. R² indica quanto a reta explica a variação. '
            'São estimativas descritivas, sem teste de significância.\n\n'
            'A tendência de uma sonda pode refletir mudanças ambientais. A tendência da '
            'diferença indica divergência relativa; sozinha não identifica qual sonda tem drift.\n\n'
            'Janelas vazias não são preenchidas. Cada tendência usa o período efetivamente '
            'coberto pela sonda; pares usam apenas as janelas comuns. A variação ajustada '
            'se refere do primeiro ao último ponto válido. Alterar a janela pode alterar os resultados.', parent=self)

    def show_analysis(self, payload, mode, zone, first, last):
        result, skipped = payload
        info = parameter_info(result['parameter'])
        du = info['delta_unit']
        self.status.set(f"Análise concluída: janela de {result['step']:g} s; {skipped} valores não finitos ignorados.")
        window = tk.Toplevel(self)
        window.title(info['label'] + ' — ' + ('similaridade entre pares' if mode == 'similarity' else 'tendências'))
        window.geometry('1200x820')
        note = f"{first:%d/%m/%Y %H:%M:%S} a {last:%d/%m/%Y %H:%M:%S} (fim exclusivo) — {zone.key}\n"
        note += f"Janela={result['step']:g} s | Offset={result['offset_mode']} | Tolerância={result['tolerance']:g} {du}/h"
        ttk.Label(window, text=note, padding=8).pack(fill='x')
        notebook = ttk.Notebook(window)
        notebook.pack(fill='both', expand=True)
        summary = ttk.Frame(notebook, padding=8)
        notebook.add(summary, text='Resumo')
        if mode == 'similarity':
            headers = ['Par A → B', 'Janelas comuns', 'Cobertura (%)', f'Offset ({du})', f'Média B-A ({du})',
                       f'RMSE ({du})', f'MAE ({du})', 'Pearson r', f'Diferença ({du}/h)', 'R² diferença', 'Direção', 'Observação']
            rows, plots = [], []
            for i, item in enumerate(result['pairs']):
                if 'error' in item:
                    rows.append([item['label'], item['n'], fmt(100*item['n']/result['size']), *(['—']*8), item['error']])
                else:
                    trend = item['trend']
                    rows.append([item['label'], item['n'], fmt(100*item['n']/result['size']), fmt(item['offset']), fmt(item['raw_mean']),
                                 fmt(item['rmse']), fmt(item['mae']), fmt(item['correlation']), fmt(trend['slope']),
                                 fmt(trend['r2']), trend['direction'], ''])
                    plots.append((i, item['label']))
            explanation = 'Diferença = (B − offset) − A. RMSE/MAE após ajuste. Cobertura: janelas comuns / janelas do período.\nInclinação da diferença: divergência relativa. Correlação indefinida para séries constantes.'
        else:
            headers = ['Sonda', 'Leituras', 'Janelas válidas', 'Cobertura (%)', 'Horas cobertas',
                       f'{du}/h', f'{du}/dia', f'Variação ajustada ({du})', 'R²', 'Direção', 'Observação']
            rows, plots = [], []
            for sid, item in result['series'].items():
                trend = item['trend']
                label = f"{item['name']} [{sid}]"
                if trend is None:
                    rows.append([label, item['raw_n'], item['n'], fmt(100*item['n']/result['size']), *(['—']*6), 'Menos de 3 janelas válidas.'])
                else:
                    rows.append([label, item['raw_n'], item['n'], fmt(100*item['n']/result['size']), fmt(trend['hours']),
                                 fmt(trend['slope']), fmt(trend['slope']*24), fmt(trend['change']), fmt(trend['r2']), trend['direction'], ''])
                    plots.append((sid, label))
            explanation = 'Regressão linear das médias por janela; cada janela tem peso igual. Cobertura: janelas válidas / total.\nTendência descritiva do parâmetro: não comprova drift instrumental. Taxa diária = taxa horária × 24; não é previsão.'
        ttk.Label(summary, text=explanation, wraplength=1100).pack(fill='x', pady=8)
        table_frame = ttk.Frame(summary)
        table_frame.pack(fill='both', expand=True)
        columns = [str(i) for i in range(len(headers))]
        table = ttk.Treeview(table_frame, columns=columns, show='headings')
        ys = ttk.Scrollbar(table_frame, orient='vertical', command=table.yview)
        xs = ttk.Scrollbar(table_frame, orient='horizontal', command=table.xview)
        table.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        table.grid(row=0, column=0, sticky='nsew')
        ys.grid(row=0, column=1, sticky='ns')
        xs.grid(row=1, column=0, sticky='ew')
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        for i, header in enumerate(headers):
            table.heading(str(i), text=header)
            table.column(str(i), width=270 if i == 0 else 140, stretch=False)
        for row in rows:
            table.insert('', 'end', values=row)
        actions = ttk.Frame(summary)
        actions.pack(fill='x', pady=8)
        ttk.Button(actions, text='Exportar resumo CSV', command=lambda: self.export_summary(headers, rows, result, first, last, zone)).pack(side='left', padx=5)
        ttk.Button(actions, text='Exportar séries CSV', command=lambda: self.export_series(result, first, last, zone)).pack(side='left', padx=5)
        # Figuras criadas apenas ao abrir cada aba, para evitar travar com muitos pares.
        pending = {}
        for key, label in plots:
            frame = ttk.Frame(notebook)
            notebook.add(frame, text=label)
            pending[str(frame)] = (frame, key)
        def open_tab(event):
            entry = pending.pop(notebook.select(), None)
            if entry:
                frame, key = entry
                try:
                    fig = analysis_figure(result, key, mode, zone, first, last)
                    self.embed_figure(frame, fig)
                except Exception as exc:
                    pending[str(frame)] = entry
                    messagebox.showerror('Erro no gráfico', str(exc), parent=window)
        notebook.bind('<<NotebookTabChanged>>', open_tab)

    def export_summary(self, headers, rows, result, first, last, zone):
        path = filedialog.asksaveasfilename(parent=self, defaultextension='.csv', initialfile=f"resumo_{result['parameter']}.csv", filetypes=[('CSV', '*.csv')])
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8-sig', newline='') as output:
                writer = csv.writer(output, delimiter=';')
                writer.writerow(['Inicio', first.isoformat(), 'Fim_exclusivo', last.isoformat(), 'Fuso', zone.key])
                info = parameter_info(result['parameter'])
                writer.writerow(['Parametro', result['parameter'], 'Nome', info['label'], 'Unidade', info['unit'], 'Unidade_diferenca', info['delta_unit']])
                writer.writerow(['Janela_segundos', result['step'], 'Offset', result['offset_mode'], f"Tolerancia_{info['delta_unit']}/h", result['tolerance']])
                writer.writerow(headers)
                writer.writerows(rows)
            messagebox.showinfo('Exportação', 'Resumo salvo.', parent=self)
        except OSError as exc:
            messagebox.showerror('Exportação', str(exc), parent=self)

    def export_series(self, result, first, last, zone):
        path = filedialog.asksaveasfilename(parent=self, defaultextension='.csv', initialfile=f"series_{result['parameter']}.csv", filetypes=[('CSV', '*.csv')])
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8-sig', newline='') as output:
                writer = csv.writer(output, delimiter=';')
                writer.writerow(['Inicio', first.isoformat(), 'Fim_exclusivo', last.isoformat(), 'Fuso', zone.key])
                info = parameter_info(result['parameter'])
                writer.writerow(['Parametro', result['parameter'], 'Nome', info['label'], 'Unidade', info['unit'], 'Unidade_diferenca', info['delta_unit']])
                writer.writerow(['Janela_segundos', result['step'], 'Offset', result['offset_mode'], f"Tolerancia_{info['delta_unit']}/h", result['tolerance']])
                series = list(result['series'].items())
                pairs = [item for item in result['pairs'] if 'error' not in item]
                writer.writerow([f"Offsets ({info['delta_unit']})"] + [f"{item['label']}: {item['offset']:.12g}" for item in pairs])
                writer.writerow(['Centro_janela'] + [f"{item['name']} [{sid}] media ({info['unit']})" for sid, item in series]
                                + [f"{item['label']} diferenca_ajustada ({info['delta_unit']})" for item in pairs])
                for i, date in enumerate(result['dates']):
                    values = [item['values'][i] for _, item in series] + [item['delta'][i] for item in pairs]
                    writer.writerow([date.isoformat()] + [float(value) if np.isfinite(value) else '' for value in values])
            messagebox.showinfo('Exportação', 'Séries salvas. Campos vazios indicam ausência de dados.', parent=self)
        except OSError as exc:
            messagebox.showerror('Exportação', str(exc), parent=self)

    def show_plots(self, result, mode, zone, first, last, parameter='co2_ppm'):
        info = parameter_info(parameter)
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
        window.title(info['label'] + ' — ' + ('superpostos' if mode == 'overlay' else 'isolados'))
        window.geometry('1050x740')
        if mode == 'overlay':
            self.plot_frame(window, populated, zone, first, last, parameter)
        else:
            notebook = ttk.Notebook(window)
            notebook.pack(fill='both', expand=True)
            for sid, record in populated:
                frame = ttk.Frame(notebook)
                notebook.add(frame, text=f'{record[0]} [ID: {sid}]')
                self.plot_frame(frame, [(sid, record)], zone, first, last, parameter)

    def plot_frame(self, parent, series, zone, first, last, parameter='co2_ppm'):
        info = parameter_info(parameter)
        fig = Figure(figsize=(10, 6), dpi=100, layout='constrained')
        ax = fig.add_subplot(111)
        for sid, (name, dates, values) in series:
            ax.plot(dates, values, linewidth=1, label=f'{name} [ID: {sid}]')
        ax.set(title=info['label'], xlabel=f'Data e hora — {zone.key}', ylabel=f"{info['label']} ({info['unit']})")
        ax.set_xlim(first, last)
        ax.grid(True, alpha=0.3)
        ax.legend(loc='best', fontsize=9)
        locator = mdates.AutoDateLocator(tz=zone)
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator, tz=zone))
        self.embed_figure(parent, fig)

    def embed_figure(self, parent, fig):
        canvas = FigureCanvasTkAgg(fig, master=parent)
        toolbar = NavigationToolbar2Tk(canvas, parent, pack_toolbar=False)
        toolbar.update()
        toolbar.pack(side='bottom', fill='x')
        canvas.get_tk_widget().pack(fill='both', expand=True)
        canvas.draw()
        parent._plot_refs = (fig, canvas, toolbar)


if __name__ == '__main__':
    App().mainloop()
