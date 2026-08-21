#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Wed Aug 19 09:25:49 2026

@author: Casimiro de Almeida Barreto (cdab63)
@email: casimiro.barreto@gmail.com
@copyright: Casimiro de Almeida Barreto
@license: Apache 2.0
"""

import datetime
import logging
import psycopg
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------
def format_datetime(dt):
    if isinstance(dt, datetime.datetime):
        return dt.strftime('%Y/%m/%d %H:%M:%S.%f')[:-3] + dt.strftime('%z')
    return dt

def to_placeholders(a_list):
    """
    Converte entrada para uma lista de inteiros (para uso com ANY) e uma lista
    de strings (para outros usos, se necessário).
    Sempre retorna uma lista para o primeiro valor.
    """
    if isinstance(a_list, (list, tuple)):
        if not all(isinstance(x, int) for x in a_list):
            raise ValueError(f'Um elemento não inteiro em {a_list}')
        # Converte para lista explicitamente
        return list(a_list), [str(a) for a in a_list]
    elif isinstance(a_list, int):
        return [a_list], [str(a_list)]
    elif a_list is None:
        return [], []
    else:
        raise ValueError('Teria que ser lista ou tupla')

# ---------------------------------------------------------
class ProbesDatabase:
    def __init__(self):
        self.conn = None
        self.probes = None
        self.probesAsPandas = None

    def connect(self, db_name, db_host, db_port, db_user, db_password):
        try:
            self.conn = psycopg.connect(
                host=db_host, dbname=db_name, user=db_user,
                password=db_password, port=db_port, autocommit=True
            )
        except Exception as e:
            logger.error(f"Falha ao abrir {db_name} em {db_host}:{db_port} para {db_user}")
            logger.info(f"Info: {str(e)}")
            self.conn = None
        return self.conn

    def disconnect(self):
        try:
            self.conn.disconnect()
        except Exception as e:
            logger.debug("Falha na desconexão da base de dados.")
            logger.debug(f"Info: {str(e)}")
        self.conn = None

    def getRawProbesList(self):
        cmd = """
            SELECT id, device_id, sensor_name, sensor_type, active, last_seen
            FROM sensor
            ORDER BY id
        """
        try:
            with self.conn.cursor() as cur:
                cur.execute(cmd)
                return cur.fetchall()
        except Exception as e:
            logger.error(f"Erro ao pegar lista de sondas: {str(e)}")
            return None

    def getProbesList(self):
        self.probes = {}
        raw = self.getRawProbesList()
        if raw is None:
            return None
        for row in raw:
            self.probes[row[0]] = [format_datetime(item) for item in row[1:]]
        return self.probes

    def probesListToPandas(self):
        if self.probes is None:
            return None
        try:
            pap = pd.DataFrame.from_dict(
                self.probes, orient='index',
                columns=['DevID', 'Nome', 'Tipo', 'Ativo', 'Timestamp']
            )
            self.probesAsPandas = pap.reset_index().rename(columns={'index': 'ID'})
        except Exception as e:
            logger.error(f"Erro serializando para pandas {str(e)}")
            return None
        return self.probesAsPandas

    def getProbeCoordinates(self, id):
        cmd = "SELECT latitude, longitude, altitude FROM sensor WHERE id = %s"
        try:
            with self.conn.cursor() as cur:
                cur.execute(cmd, (id,))
                return cur.fetchone()
        except Exception as e:
            logger.error(f"Erro ao pegar coordenadas de: {id}")
            logger.info(f"Info: {str(e)}")
            return None

    # --------------------------------------------------------------
    # Método genérico – com correção: ids_list é sempre uma lista
    def _get_readings(self, ids, start, end, type, module,
                      column, alias, not_null_condition, limit=None):
        """
        Parâmetros:
          ids       : int ou lista/tupla de ints
          start     : datetime ou None (opcional)
          end       : datetime ou None (opcional)
          type      : 'gas', 'weather' ou 'any'
          module    : 'sdc', 'bme' ou 'any'
          column    : nome da coluna na tabela sensor_reading (ex: 'co2_ppm')
          alias     : nome a ser usado na saída (apenas documentação)
          not_null_condition : condição extra para garantir NOT NULL
          limit     : int ou None (se fornecido, retorna os N registros MAIS RECENTES)
        Retorna:
          lista de tuplas (sensor_id, valor_lido, reading_time) ou None em caso de erro.
        """
        # Normaliza ids: obtém uma lista de inteiros (obrigatoriamente)
        try:
            ids_list, _ = to_placeholders(ids)
            # Garantia extra: se ainda for tupla (não deveria), converte
            if not isinstance(ids_list, list):
                ids_list = list(ids_list)
        except Exception as e:
            logger.error(str(e))
            return None

        # Monta condições dinâmicas
        conditions = [not_null_condition]

        if type == 'gas':
            conditions.append("s.sensor_type = 'gas'")
        elif type == 'weather':
            conditions.append("s.sensor_type = 'weather'")
        # type == 'any' -> sem filtro

        if module == 'sdc':
            conditions.append("r.co2_ppm IS NOT NULL")
        elif module == 'bme':
            conditions.append("r.co2_ppm IS NULL")
        # module == 'any' -> sem filtro

        # Condições de data (opcionais)
        if start is not None:
            conditions.append("r.reading_time >= %s")
        if end is not None:
            conditions.append("r.reading_time <= %s")

        # Ordenação e LIMIT
        if limit is not None:
            order_by = "ORDER BY r.reading_time DESC"
            limit_clause = "LIMIT %s"
        else:
            order_by = "ORDER BY r.reading_time"
            limit_clause = ""

        # Monta a query: placeholders na ordem: datas, ANY, LIMIT
        cmd = f"""
            SELECT s.id, r.{column}, r.reading_time
            FROM sensor_reading r
            JOIN sensor s ON s.id = r.sensor_id
            WHERE { ' AND '.join(conditions) }
              AND s.id = ANY(%s)
            {order_by} {limit_clause}
        """

        # Parâmetros na ordem correta
        params = []
        if start is not None:
            params.append(start)
        if end is not None:
            params.append(end)
        params.append(ids_list)   # agora é sempre uma lista
        if limit is not None:
            params.append(limit)

        try:
            with self.conn.cursor() as cur:
                cur.execute(cmd, tuple(params))
                res = cur.fetchall()
                # Formata as datas (terceiro elemento)
                return [[format_datetime(dt) if idx == 2 else dt for idx, dt in enumerate(row)]
                        for row in res]
        except Exception as e:
            logger.error(f"Erro ao pegar {column} para {ids} no período {start} a {end}")
            logger.info(f"Info: {str(e)}")
            return None

    # ---------------------------------------------------------
    # Métodos públicos – com limit opcional
    def getCO2(self, ids, start=None, end=None, limit=None):
        return self._get_readings(
            ids, start, end,
            type='gas',
            module='sdc',
            column='co2_ppm',
            alias='co2_ppm',
            not_null_condition="r.co2_ppm IS NOT NULL",
            limit=limit
        )

    def getPressure(self, ids, start=None, end=None, type='any', module='any', limit=None):
        return self._get_readings(
            ids, start, end,
            type=type,
            module=module,
            column='absolute_pressure_kpa',
            alias='pressão_kPa',
            not_null_condition="r.absolute_pressure_kpa IS NOT NULL",
            limit=limit
        )
    
    def getGaugePressure(self, ids, start=None, end=None, type='any', module='any', limit=None):
        return self._get_readings(
            ids, start, end,
            type=type,
            mmodule=module,
            column='gauge_pressure_kpa',
            alias='pressão_barométrica_kpa',
            not_null_condition='r.gauge_pression_kpa IS NOT NULL',
            limit=limit
        )

    def getTemperature(self, ids, start=None, end=None, type='any', module='any', limit=None):
        return self._get_readings(
            ids, start, end,
            type=type,
            module=module,
            column='temperature_c',
            alias='temperatura_C',
            not_null_condition="r.temperature_c IS NOT NULL",
            limit=limit
        )

    def getHumidity(self, ids, start=None, end=None, type='any', module='any', limit=None):
        return self._get_readings(
            ids, start, end,
            type=type,
            module=module,
            column='humidity_percent',
            alias='umidade_por_cento',
            not_null_condition="r.humidity_percent IS NOT NULL",
            limit=limit
        )
    
    def getRSSI(self, ids, start=None, end=None, type='any', module='any', limit=None):
        return self._get_readings(
            ids, start, end,
            type=type,
            module=module,
            column='rssi',
            alias='rssi',
            not_null_condition='r.rssi IS NOT NULL',
            limit=limit)
    
    def getO2(self, ids, start=None, end=None, type='any', module='any', limit=None):
        return self._get_readings(
            ids, start, end,
            type=type,
            module=module,
            column='o2_percent',
            alias='o2_por_cento',
            not_null_condition='r.o2_percent IS NOT NULL',
            limit=limit)
    
    def getCH4(self, ids, start=None, end=None, type='any', module='any', limit=None):
        return self._get_readings(
            ids, start, end,
            type=type,
            module=module,
            column='ch4_percent_lel',
            alias='ch4_por_cento',
            not_null_condition='r.ch4_percent_lel IS NOT NULL',
            limit=limit)

    def getCO(self, ids, start=None, end=None, type='any', module='any', limit=None):
        return self._get_readings(
            ids, start, end,
            type=type,
            module=module,
            column='co_ppm',
            alias='co_ppm',
            not_null_condition='r.co_ppm IS NOT NULL',
            limit=limit)

    def getH2S(self, ids, start=None, end=None, type='any', module='any', limit=None):
        return self._get_readings(
            ids, start, end,
            type=type,
            module=module,
            column='h2s_ppm',
            alias='h2s_ppm',
            not_null_condition='r.h2s_ppm IS NOT NULL',
            limit=limit)
    
    def getNH3(self, ids, start=None, end=None, type='any', module='any', limit=None):
        return self._get_readings(
            ids, start, end,
            type=type,
            module=module,
            column='nh3_ppm',
            alias='nh3_ppm',
            not_null_condition='r.nh3_ppm IS NOT NULL',
            limit=limit)
    
    def getN2O(self, ids, start=None, end=None, type='any', module='any', limit=None):
        return self._get_readings(
            ids, start, end,
            type=type,
            module=module,
            column='n2o_ppm',
            alias='n2o_ppm',
            not_null_condition='r.n2o_ppm IS NOT NULL',
            limit=limit)
    
    def getBatteryVoltage(self, ids, start=None, end=None, type='any', module='any', limit=None):
        return self._get_readings(
            ids, start, end,
            type=type,
            module=module,
            column='battery_voltage',
            alias='tensão_na_bateria',
            not_null_condition='r.battery_voltage IS NOT NULL',
            limit=limit)
    
    def getBatterySoC(self, ids, start=None, end=None, type='any', module='any', limit=None):
        return self._get_readings(
            ids, start, end,
            type=type,
            module=module,
            column='battery_soc_percent',
            alias='estado_de_carga_da_bateria',
            not_null_condition='r.battery_soc_percent IS NOT NULL',
            limit=limit)
    
    def getBatterySoH(self, ids, start=None, end=None, type='any', module='any', limit=None):
        return self._get_readings(
            ids, start, end,
            type=type,
            module=module,
            column='battery_soh_percent',
            alias='estado_de_saúde_da_bateria',
            not_null_condition='r.battery_soh_percent IS NOT NULL',
            limit=limit)
    
    # ---------------------------------------------------------
    # Versões em Pandas – com limit opcional
    
    def getGaugePressureAsPandas(self, ids, start=None, end=None, type='any', module='any', limit=None):
        res = self.getGaugePressure(ids, start, end, type, module, limit)
        if res is not None:
            return pd.DataFrame(res, columns=['sonda', 'gauge_pressure_kpa', 'timestamp'])
        return None
    
    def getBatterySoHasPandas(self, ids, start=None, end=None, type='any', module='any', limit=None):
        res = self.getBatterySoH(ids, start, end, type, module, limit)
        if res is not None:
            return pd.DataFrame(res, columns=['sonda', 'battery_soh_percent', 'timestamp'])
        return None
    
    def getBatterySoCasPandas(self, ids, start=None, end=None, type='any', module='any', limit=None):
        res = self.getBatterySoC(ids, start, end, type, module, limit)
        if res is not None:
            return pd.DataFrame(res, columns=['sonda', 'battery_soc_percent', 'timestamp'])
        return None
    
    def getBatteryVoltageAsPandas(self, ids, start=None, end=None, type='any', module='any', limit=None):
        res = self.getBatteryVoltage(ids, start, end, type, module, limit)
        if res is not None:
            return pd.DataFrame(res, columns=['sonda', 'tensão da bateria', 'timestamp'])
        return None
    
    def getN2OasPandas(self, ids, start=None, end=None, type='any', module='any', limit=None):
        res = self.getN2O(ids, start, end, type, module, limit)
        if res is not None:
            return pd.DataFrame(res, columns=['sonda', 'n2o_ppm', 'timestamp'])
        return None
    
    def getNH3asPandas(self, ids, start=None, end=None, type='any', module='any', limit=None):
        res = self.getNH3(ids, start, end, type, module, limit)
        if res is not None:
            return pd.DataFrame(res, columns=['sonda', 'nh3_ppm', 'timestamp'])
        return None
    
    def getH2SasPandas(self, ids, start=None, end=None, type='any', module='any', limit=None):
        res = self.getH2S(ids, start, end, type, module, limit)
        if res is not None:
            return pd.DataFrame(res, columns=['sonda', 'h2s_ppm', 'timestamp'])
        return None
    
    def getCOasPandas(self, ids, start=None, end=None, type='any', module='any', limit=None):
        res = self.getCO(ids, start, end, type, module, limit)
        if res is not None:
            return pd.DataFrame(res, columns=['sonda', 'co_ppm', 'timestamp'])
        return None
    
    def getCH4asPandas(self, ids, start=None, end=None, type='any', module='any', limit=None):
        res = self.getCH4(ids, start, end, type, module, limit)
        if res is not None:
            return pd.DataFrame(res, columns=['sonda', 'ch4_percent_lel', 'timestamp'])
        return None
    
    def getO2asPandas(self, ids, start=None, end=None, type='any', module='any', limit=None):
        res = self.getO2(ids, start, end, type, module, limit)
        if res is not None:
            return pd.DataFrame(res, columns=['sonda', 'o2_percent', 'timestamp'])
        return None
    
    def getCO2asPandas(self, ids, start=None, end=None, limit=None):
        res = self.getCO2(ids, start, end, limit)
        if res is not None:
            return pd.DataFrame(res, columns=['sonda', 'co2_ppm', 'timestamp'])
        return None

    def getPressureAsPandas(self, ids, start=None, end=None, type='any', module='any', limit=None):
        res = self.getPressure(ids, start, end, type, module, limit)
        if res is not None:
            return pd.DataFrame(res, columns=['sonda', 'pressão_kPa', 'timestamp'])
        return None

    def getTemperatureAsPandas(self, ids, start=None, end=None, type='any', module='any', limit=None):
        res = self.getTemperature(ids, start, end, type, module, limit)
        if res is not None:
            return pd.DataFrame(res, columns=['sonda', 'temperatura_C', 'timestamp'])
        return None

    def getHumidityAsPandas(self, ids, start=None, end=None, type='any', module='any', limit=None):
        res = self.getHumidity(ids, start, end, type, module, limit)
        if res is not None:
            return pd.DataFrame(res, columns=['sonda', 'umidade_por_cento', 'timestamp'])
        return None
    
    def getRSSIasPandas(self, ids, start=None, end=None, type='any', module='any', limit=None):
        res = self.getRSSI(ids, start, end, type, module,limit)
        if res is not None:
            return pd.DataFrame(res, columns=['sonda', 'rssi', 'timestamp'])
        return None
    
    # ---------------------------------------------------------------
    # Consulta mista: múltiplas grandezas em uma única chamada
    def getMultiReadings(self, ids, start=None, end=None, variables=None, type=None, module=None, limit=None):
        """
        Retorna um DataFrame com leituras de múltiplas grandezas para as sondas especificadas.

        Parâmetros:
          ids       : int ou lista/tupla de ints (IDs das sondas)
          start     : datetime ou None (início do intervalo)
          end       : datetime ou None (fim do intervalo)
          variables : lista de strings com as grandezas desejadas.
                      Opções: 'co2', 'temp', 'press', 'hum'.
                      Se None, retorna todas as disponíveis.
          limit     : int ou None. Se fornecido, retorna apenas os N registros mais recentes
                      (considerando o conjunto completo de leituras).

        Retorna:
          pandas.DataFrame com colunas:
            - sonda   : ID da sonda
            - timestamp : data/hora da leitura
            - co2_ppm (se solicitado)
            - temperature_c (se solicitado)
            - absolute_pressure_kpa (se solicitado)
            - humidity_percent (se solicitado)
        """
        # Mapeamento de nomes amigáveis para colunas do banco
        var_map = {
            'co2':   'co2_ppm',
            'temp':  'temperature_c',
            'press': 'absolute_pressure_kpa',
            'hum':   'humidity_percent',
            'rssi':  'rssi'
        }

        # Se variables for None, usa todas
        if variables is None:
            variables = list(var_map.keys())

        # Valida e obtém os nomes das colunas
        cols = []
        for v in variables:
            if v not in var_map:
                raise ValueError(f"Variável '{v}' inválida. Opções: {list(var_map.keys())}")
            cols.append(var_map[v])

        # Se não houver variáveis, retorna DataFrame vazio
        if not cols:
            return pd.DataFrame()

        # Normaliza ids para lista
        try:
            ids_list, _ = to_placeholders(ids)
            if not isinstance(ids_list, list):
                ids_list = list(ids_list)
        except Exception as e:
            logger.error(str(e))
            return None

        # Construção da query
        # Selecionamos s.id, r.reading_time, e as colunas desejadas
        select_cols = ["s.id AS sonda", "r.reading_time AS timestamp"] + [f"r.{col}" for col in cols]
        select_clause = ", ".join(select_cols)

        conditions = []
        conditions.append("s.id = ANY(%s)")

        if start is not None:
            conditions.append("r.reading_time >= %s")
        if end is not None:
            conditions.append("r.reading_time <= %s")
            
        if type is not None:
            conditions.append("s.sensor_type = %s")
        if module == 'sdc':
            conditions.append("r.co2_ppm IS NOT NULL")
        elif module == 'bme':
            conditions.append("r.co2_ppm IS NULL")
        else:
            raise ValueError(f"Module must be sdc or bme but: {module}")

        where_clause = " AND ".join(conditions)

        # Ordenação e LIMIT
        if limit is not None:
            order_by = "ORDER BY r.reading_time DESC"
            limit_clause = "LIMIT %s"
        else:
            order_by = "ORDER BY r.reading_time"
            limit_clause = ""

        cmd = f"""
            SELECT {select_clause}
            FROM sensor_reading r
            JOIN sensor s ON s.id = r.sensor_id
            WHERE {where_clause}
            {order_by} {limit_clause}
        """

        # Monta parâmetros na ordem correta
        params = []
        params.append(ids_list)   # para ANY
        if start is not None:
            params.append(start)
        if end is not None:
            params.append(end)
        if limit is not None:
            params.append(limit)

        try:
            with self.conn.cursor() as cur:
                cur.execute(cmd, tuple(params))
                res = cur.fetchall()

                # Converte para DataFrame
                df = pd.DataFrame(res, columns=[desc.name for desc in cur.description])

                # Formata timestamps (opcional: já vem como datetime, mas podemos converter)
                if 'timestamp' in df.columns:
                    df['timestamp'] = df['timestamp'].apply(format_datetime)

                return df

        except Exception as e:
            logger.error(f"Erro na consulta mista para {ids} no período {start} a {end}")
            logger.info(f"Info: {str(e)}")
            return None
        
# -----------------------------------------------------------------------------

def main():
    
    # -------------------------------------------------------------------------
    # TESTES UNITÁRIOS PARA A CLASSE
    # -------------------------------------------------------------------------
    
    # Create the instance of the class
    pdb = ProbesDatabase()

    # Test connection
    pdb.connect('painel_de_dados_db', 'localhost', 5432, 'grafana', '1cbe991a14')
    
    # Test getting sensors from sensor table
    print("THE SENSORS")
    sensors = pdb.getProbesList()
    print(f"{sensors}")
    
    # Test creating a pandas structure
    print("THE SENSORS PANDAS")
    sap = pdb.probesListToPandas()
    print(f"{sap}")
    
    # Teste latitude, longitude, altitude
    print("LATITUDE LONGITUDE ALTITUDE")
    coord = pdb.getProbeCoordinates(22)
    print(f"{coord}")
    
    # teste pegar co2
    print("CO2 LIST")
    co2 = pdb.getCO2((22, 26, 28), start="2026/08/10 0:0:0", end="2026/08/18 23:59:59", limit=10)
    print(f"{co2}")
    
    # teste co2 sem datas
    print("CO2 LIST SEM DATA")
    co2 = pdb.getCO2((22, 26, 28), limit=10)
    print(f"{co2}")
    
    print("CO2 LIST PANDAS")
    co2 = pdb.getCO2asPandas((22, 26, 28), start="2026/08/10 0:0:0", end="2026/08/18 23:59:59", limit=10)
    print(f"{co2}")
    
    print("CO2 SINGLE PROBE")
    co2 = pdb.getCO2(22, start="2026/08/10 0:0:0", end="2026/08/18 23:59:59", limit=10)
    print(f"{co2}")
    
    print("CO2 SINGLE PROBE PANDAS")
    co2 = pdb.getCO2asPandas(22, start="2026/08/10 0:0:0", end="2026/08/18 23:59:59", limit=10)
    print(f"{co2}")
    
    # teste pegar pressão absoluta kpa
    print("PRESSURE SINGLE PROBE")
    pkpa = pdb.getPressure(22, start="2026/08/10 0:0:0", end="2026/08/18 23:59:59", limit=10)
    print(f"{pkpa}")
    
    # teste para pegar pressão absoluta kpa
    print("PRESSURE LIST")
    pkpa = pdb.getPressure((22, 26, 28), start="2026/08/10 0:0:0", end="2026/08/18 23:59:59", module="bme", limit=10)
    print(f"{pkpa}")
    
    # teset para pegar pressão absoluta pandas
    print("PRESSURE LIST PANDAS")
    pkpa = pdb.getPressureAsPandas((22, 26, 28), start="2026/08/10 0:0:0", end="2026/08/18 23:59:59", module="bme", limit=10)
    print(f"{pkpa}")
    
    # teste pegar temperatura C
    print("TEMPERATURE SINGLE PROBE")
    ptemp = pdb.getTemperature(22, start="2026/08/10 0:0:0", end="2026/08/18 23:59:59", limit=10)
    print(f"{ptemp}")
    
    # teste para pegar temperatura PANDAS
    print("TEMPERATURE SINGLE PROBE PANDAS")
    ptemp = pdb.getTemperatureAsPandas(22, start="2026/08/10 0:0:0", end="2026/08/18 23:59:59", module="bme", limit=10)
    print(f"{ptemp}")
    
    # teste para pegar teperatura listas
    print("TEMPERATURE LIST")
    ptemp = pdb.getTemperature((22, 26, 28), start="2026/08/10 0:0:0", end="2026/08/18 23:59:59", module="bme", limit=10)
    print(f"{ptemp}")
    
    # teste para pegar temperatura listas pandas
    print("TEMPERATURE LISTS PANDAS")
    ptemp = pdb.getTemperatureAsPandas((22, 26, 28), start="2026/08/10 0:0:0", end="2026/08/18 23:59:59", module="bme", limit=10)
    print(f"{ptemp}")
    
    # teste para pegar umidade (geral)
    print("UMIDADE ÚNICA SONDA")
    phum = pdb.getHumidity(22, start="2026/08/10 0:0:0", end="2026/08/18 23:59:59", module="bme", limit=10)
    print(f"{phum}")
    
    # teste para pegar umidade (pandas)
    print("UMIDADE ÚNICA SONDA PANDAS")
    phum = pdb.getHumidityAsPandas(22, start="2026/08/10 0:0:0", end="2026/08/18 23:59:59", module="bme", limit=10)
    print(f"{phum}")
    
    # teste para pegar umidade lista
    print("UMIDADE LISTA")
    phum = pdb.getHumidity((22, 26, 28), start="2026/08/10 0:0:0", end="2026/08/18 23:59:59", type='gas', module='sdc', limit=10)
    print(f"{phum}")
    
    # teste pegar umidade lista pandas
    print("UMIDADE LISTA PANDAS")
    phum = pdb.getHumidityAsPandas((22, 26, 28), start="2026/08/10 0:0:0", end="2026/08/18 23:59:59", type='gas', module='bme', limit=10)
    print(f"{phum}")
    
    # teste pegar RSSI
    print("RSSI")
    rssi = pdb.getRSSI((22, 26,28), start="2026/08/10 0:0:0", end="2026/08/20 23:59:59", type='gas', module='sdc', limit=10)
    print(f"{rssi}")
    
    # teste pegar RSSI pandas
    print("RSSI PANDAS")
    rssi = pdb.getRSSIasPandas((22, 26,28), start="2026/08/10 0:0:0", end="2026/08/20 23:59:59", type='gas', module='sdc', limit=10)
    
    # teste pandas multivariáveis
    # Obtém CO2, temperatura e pressão para as sondas 22, 26, 28
    # entre duas datas, sem limite
    print("TESTE MULTI VARIÁVEIS PANDAS")
    df = pdb.getMultiReadings(
        ids=[22, 26, 28],
        start="2026/08/10 0:0:0",
        end="2026/08/18 23:59:59",
        variables=['co2', 'temp', 'hum', 'rssi'],
        module='sdc', limit=10)
    print(f"{df}\n")
    df = pdb.getMultiReadings(
        ids=[22, 26, 28],
        start="2026/08/10 0:0:0",
        end="2026/08/18 23:59:59",
        variables=['press', 'temp', 'hum'],
        module='bme', limit=10)    
    
    print(f"{df}")
    
if __name__ == '__main__':
    
    main()
