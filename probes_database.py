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
    if isinstance(a_list, (list, tuple)):
        if not all(isinstance(x, int) for x in a_list):
            raise ValueError(f'Um elemento não inteiro em {a_list}')
        return a_list, [str(a) for a in a_list]
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

    # ---------------------------------------------------------
    # Método genérico para leituras – usado internamente
    def _get_readings(self, ids, start, end, type, module,
                      column, alias, not_null_condition):
        """
        Parâmetros:
          ids       : int ou lista de ints
          start     : datetime ou None (opcional)
          end       : datetime ou None (opcional)
          type      : 'gas', 'weather' ou 'any'
          module    : 'sdc', 'bme' ou 'any'
          column    : nome da coluna na tabela sensor_reading (ex: 'co2_ppm')
          alias     : nome a ser usado na saída
          not_null_condition : condição extra para garantir NOT NULL (ex: "r.co2_ppm IS NOT NULL")
        Retorna:
          lista de tuplas (sensor_id, valor_lido, reading_time) ou None em caso de erro.
        """
        # Normaliza ids
        try:
            _, probes = to_placeholders(ids)
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

        # Monta a query
        cmd = f"""
            SELECT s.id, r.{column}, r.reading_time
            FROM sensor_reading r
            JOIN sensor s ON s.id = r.sensor_id
            WHERE { ' AND '.join(conditions) }
              AND s.id = ANY(%s)
            ORDER BY r.reading_time
        """

        # Parâmetros para o cursor
        params = []
        if start is not None:
            params.append(start)
        if end is not None:
            params.append(end)
        params.append(probes)   # ANY(%s) espera uma lista

        try:
            with self.conn.cursor() as cur:
                cur.execute(cmd, tuple(params))
                res = cur.fetchall()
                # Formata as datas (cada tupla tem (id, valor, timestamp))
                return [[format_datetime(dt) if idx == 2 else dt for idx, dt in enumerate(row)]
                        for row in res]
        except Exception as e:
            logger.error(f"Erro ao pegar {column} para {ids} no período {start} a {end}")
            logger.info(f"Info: {str(e)}")
            return None

    # ---------------------------------------------------------
    # Métodos públicos – agora com start/end opcionais
    def getCO2(self, ids, start=None, end=None):
        return self._get_readings(
            ids, start, end,
            type='gas',   # CO2 só existe em sensores do tipo 'gas'
            module='sdc', # CO2 vem do módulo SDC
            column='co2_ppm',
            alias='co2_ppm',
            not_null_condition="r.co2_ppm IS NOT NULL"
        )

    def getPressure(self, ids, start=None, end=None, type='any', module='any'):
        return self._get_readings(
            ids, start, end,
            type=type,
            module=module,
            column='absolute_pressure_kpa',
            alias='pressão_kPa',
            not_null_condition="r.absolute_pressure_kpa IS NOT NULL"
        )

    def getTemperature(self, ids, start=None, end=None, type='any', module='any'):
        return self._get_readings(
            ids, start, end,
            type=type,
            module=module,
            column='temperature_c',
            alias='temperatura_C',
            not_null_condition="r.temperature_c IS NOT NULL"  # pequena correção: antes usava pressure, mas deve ser temperature
        )

    def getHumidity(self, ids, start=None, end=None, type='any', module='any'):
        return self._get_readings(
            ids, start, end,
            type=type,
            module=module,
            column='humidity_percent',
            alias='umidade_por_cento',
            not_null_condition="r.humidity_percent IS NOT NULL"
        )

    # ---------------------------------------------------------
    # Versões em Pandas (mantidas)
    def getCO2asPandas(self, ids, start=None, end=None):
        res = self.getCO2(ids, start, end)
        if res is not None:
            return pd.DataFrame(res, columns=['sonda', 'co2_ppm', 'timestamp'])
        return None

    def getPressureAsPandas(self, ids, start=None, end=None, type='any', module='any'):
        res = self.getPressure(ids, start, end, type, module)
        if res is not None:
            return pd.DataFrame(res, columns=['sonda', 'pressão_kPa', 'timestamp'])
        return None

    def getTemperatureAsPandas(self, ids, start=None, end=None, type='any', module='any'):
        res = self.getTemperature(ids, start, end, type, module)
        if res is not None:
            return pd.DataFrame(res, columns=['sonda', 'temperatura_C', 'timestamp'])
        return None

    def getHumidityAsPandas(self, ids, start=None, end=None, type='any', module='any'):
        res = self.getHumidity(ids, start, end, type, module)
        if res is not None:
            return pd.DataFrame(res, columns=['sonda', 'umidade_por_cento', 'timestamp'])
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
    co2 = pdb.getCO2((22, 26, 28),"2026/08/10 0:0:0", "2026/08/18 23:59:59")
    print(f"{co2[0]}")
    
    # teste co2 sem datas
    print("CO2 LIST SEM DATA")
    co2 = pdb.getCO2((22, 26, 28), None, None)
    print(f"{co2[0:9]}")
    
    print("CO2 LIST PANDAS")
    co2 = pdb.getCO2asPandas((22, 26, 28),"2026/08/10 0:0:0", "2026/08/18 23:59:59")
    print(f"{co2}")
    
    print("CO2 SINGLE PROBE")
    co2 = pdb.getCO2(22,"2026/08/10 0:0:0", "2026/08/18 23:59:59")
    print(f"{co2[0]}")
    
    print("CO2 SINGLE PROBE PANDAS")
    co2 = pdb.getCO2asPandas(22,"2026/08/10 0:0:0", "2026/08/18 23:59:59")
    print(f"{co2}")
    
    # teste pegar pressão absoluta kpa
    print("PRESSURE SINGLE PROBE")
    pkpa = pdb.getPressure(22, "2026/08/10 0:0:0", "2026/08/18 23:59:59")
    print(f"{pkpa[0]}")
    
    # teste para pegar pressão absoluta kpa
    print("PRESSURE LIST")
    pkpa = pdb.getPressure((22, 26, 28),"2026/08/10 0:0:0", "2026/08/18 23:59:59", module="bme")
    print(f"{pkpa[0]}")
    
    # teset para pegar pressão absoluta pandas
    print("PRESSURE LIST PANDAS")
    pkpa = pdb.getPressureAsPandas((22, 26, 28),"2026/08/10 0:0:0", "2026/08/18 23:59:59", module="bme")
    print(f"{pkpa}")
    
    # teste pegar temperatura C
    print("TEMPERATURE SINGLE PROBE")
    ptemp = pdb.getTemperature(22, "2026/08/10 0:0:0", "2026/08/18 23:59:59")
    print(f"{ptemp[0]}")
    
    # teste para pegar temperatura PANDAS
    print("TEMPERATURE SINGLE PROBE PANDAS")
    ptemp = pdb.getTemperatureAsPandas(22, "2026/08/10 0:0:0", "2026/08/18 23:59:59", module="bme")
    print(f"{ptemp}")
    
    # teste para pegar teperatura listas
    print("TEMPERATURE LIST")
    ptemp = pdb.getTemperature((22, 26, 28), "2026/08/10 0:0:0", "2026/08/18 23:59:59", module="bme")
    print(f"{ptemp[0]}")
    
    # teste para pegar temperatura listas pandas
    print("TEMPERATURE LISTS PANDAS")
    ptemp = pdb.getTemperatureAsPandas((22, 26, 28), "2026/08/10 0:0:0", "2026/08/18 23:59:59", module="bme")
    print(f"{ptemp}")
    
    # teste para pegar umidade (geral)
    print("UMIDADE ÚNICA SONDA")
    phum = pdb.getHumidity(22, "2026/08/10 0:0:0", "2026/08/18 23:59:59", module="bme")
    print(f"{phum[0]}")
    
    # teste para pegar umidade (pandas)
    print("UMIDADE ÚNICA SONDA PANDAS")
    phum = pdb.getHumidityAsPandas(22, "2026/08/10 0:0:0", "2026/08/18 23:59:59", module="bme")
    print(f"{phum}")
    
    # teste para pegar umidade lista
    print("UMIDADE LISTA")
    phum = pdb.getHumidity((22, 26, 28), "2026/08/10 0:0:0", "2026/08/18 23:59:59", type='gas', module='sdc')
    print(f"{phum[0]}")
    
    # teste pegar umidade lista pandas
    print("UMIDADE LISTA PANDAS")
    phum = pdb.getHumidityAsPandas((22, 26, 28), "2026/08/10 0:0:0", "2026/08/18 23:59:59", type='gas', module='bme')
    print(f"{phum}")
    
if __name__ == '__main__':
    
    main()
