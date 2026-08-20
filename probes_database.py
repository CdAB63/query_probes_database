#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Wed Aug 19 09:25:49 2026

@author: cdab63
"""

import datetime
import logging
import psycopg
import pandas as pd

# ---------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s "
        "%(levelname)s "
        "%(message)s"
    )
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------
# TRAIT
# Esta é uma função genérica para lidar com timestamps

def format_datetime(dt):
    
    if isinstance(dt, datetime.datetime):
        # Format: yyyy/mm/dd hh:mm:ss.ms+/-utc
        # %f gives microseconds (6 digits). Slice to get milliseconds (3 digits).
        return dt.strftime('%Y/%m/%d %H:%M:%S.%f')[:-3] + dt.strftime('%z')
    
    return dt

# ---------------------------------------------------------
# TRAIT
# Converte para placeholders

def to_placeholders(a_list):
    
    # verifica se é lista ou tupla
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

class ProbesDatabase():
    
    def __init__(self):
        
        self.conn = None
        self.probes = None
        self.probesAsPandas = None
    
    # -----------------------------------------------------
    
    def connect(self,
                db_name,
                db_host,
                db_port,
                db_user,
                db_password):
        
        try:
            self.conn = psycopg.connect(host=db_host,
                                        dbname=db_name,
                                        user=db_user,
                                        password=db_password,
                                        port=db_port,
                                        autocommit=True
                                        )
        except Exception as e:
            logger.error(f"Falha ao abrir {db_name} em {db_host}:{db_port} para {db_user}")
            logger.info(f"Info: {str(e)}")
            self.conn = None
            
        return self.conn
    
    # -----------------------------------------------------

    def disconnect(self):
        
        try:
            self.conn.disconnect()
        except Exception as e:
            
            logger.debug("Falha na desconexão da base de dados.")
            logger.debug(f"Info: {str(e)}")
        
        self.conn = None
        
    # -----------------------------------------------------
    # Esta função retorna a lista bruta das sondas com:
    # id, device_id, device_name, active, last_seen

    def getRawProbesList(self):
        
        cmd = """
                SELECT id, device_id, sensor_name, sensor_type, active, last_seen
                FROM sensor
                ORDER BY id
              """
              
        try:
            with self.conn.cursor() as cur:

                cur.execute(cmd)
                res = cur.fetchall()
                
                return res
        
        except Exception as e:
            logger.error(f"Erro ao pegar lista de sondas: {str(e)}")
            return None
        
    # -----------------------------------------------------
    
    def getProbesList(self):
        
        self.probes = {}
        
        raw = self.getRawProbesList()
        if raw is None:
            return None
        
        for row in raw:
            self.probes[row[0]] = [format_datetime(item) for item in row[1:]]
            
        return self.probes
    
    # -----------------------------------------------------
    
    def probesListToPandas(self):
        
        if self.probes is None:
            return None
        
        try:
            
            pap = pd.DataFrame.from_dict(self.probes, 
                                         orient='index', 
                                         columns=['DevID', 'Nome', 
                                                  'Tipo', 'Ativo', 
                                                  'Timestamp'])
        
            self.probesAsPandas = pap.reset_index().rename(columns={'index': 'ID'})
        
        except Exception as e:
            
            logger.error(f"Erro serializando para pandas {str(e)}")
            return None
        
        return self.probesAsPandas
    
    # -----------------------------------------------------
    
    def getProbeCoordinates(self, id):
        
        cmd = """
                SELECT latitude, longitude, altitude
                FROM sensor
                WHERE id = %s
              """  
        
        try:
            with self.conn.cursor() as cur:

                cur.execute(cmd, (id,))
                res = cur.fetchone()
        
        except Exception as e:
            logger.error(f"Erro ao pegar coordenadas de: {id}")
            logger.info(f"Info: {str(e)}")
            return None

        return res
    
    # ---------------------------------------------------------------
    # get CO2 readings
    
    def getCO2(self, ids, start, end):
        
        res = None
        
        # Normaliza ids para lista (caso seja um único int)
        try:
            _, probes = to_placeholders(ids)
        except Exception as e:
            logger.error({str(e)})
            return None
            
        cmd = """
                SELECT s.id, r.co2_ppm, r.reading_time
                FROM sensor_reading r JOIN sensor s ON s.id = r.sensor_id
                WHERE
                    r.co2_ppm IS NOT NULL AND
                    s.id = ANY(%s) AND
                    s.sensor_type = 'gas' AND
                    r.reading_time >= %s AND
                    r.reading_time <= %s
                ORDER BY r.reading_time
              """
              
        try:
            
            with self.conn.cursor() as cur:
                
                cur.execute(cmd,(probes, start, end,))
                res = cur.fetchall()
                res = [ [ format_datetime(dt) for dt in item ] for item in res ]
                
        except Exception as e:
            
            logger.error(f"Erro ao pegar CO2 para {id} no período {start}, {end}")
            logger.info(f"Info: {str(e)}")
            
        return res
    
    # ---------------------------------------------------------------
    # get CO2 como pandas
    
    def getCO2asPandas(self, id, start, end):
        
        res = self.getCO2(id, start, end)
        
        if res is not None:
            
            res = pd.DataFrame(res, columns=['id', 'CO2 ppm', 'timestamp'])
            
        return res
        
    # ---------------------------------------------------------------
    # get pressure readings
    
    def getPressure(self, ids, start, end, type='any', module='any'):
        
        res = None

        # Normaliza ids para lista (caso seja um único int)
        try:
            _, probes = to_placeholders(ids)
        except Exception as e:
            logger.error({str(e)})
            return None
        
        if type == 'gas':
            cond_1 = """
                    s.sensor_type = 'gas' AND
                     """
        elif type == 'weather':
            cond_1 = """
                    s.sensor_type = 'weather' AND
                     """
        elif type == 'any':
            cond_1 = None
        else:
            logger.error(f"Tipo de sensor inválido: {type}")
            return None
        
        if module == 'sdc':
            cond_2 = """
                    r.co2_ppm IS NOT NULL AND
                      """  
        elif module == 'bme':
            cond_2 = """
                    r.co2_ppm IS NULL AND
                      """  
        elif module == 'any':
            cond_2 = None
        else:
            logger.error(f"Tipo de módulo inválido {module}")
            return None
        
        cmd = """
                SELECT s.id, r.absolute_pressure_kpa, r.reading_time
                FROM sensor_reading r JOIN sensor s ON s.id = r.sensor_id
                WHERE
                    r.absolute_pressure_kpa IS NOT NULL AND
              """
        cmd_tail = """
                    s.id = ANY(%s) AND
                    r.reading_time >= %s AND
                    r.reading_time <= %s
                ORDER BY r.reading_time    
              """
              
        
        if cond_1 is not None:
            cmd = cmd + cond_1
            
        if cond_2 is not None:
            cmd = cmd + cond_2
            
        cmd = cmd + cmd_tail
        
        try:
            
            with self.conn.cursor() as cur:
                
                cur.execute(cmd,(probes, start, end,))
                res = cur.fetchall()
                res = [ [ format_datetime(dt) for dt in item ] for item in res ]
                
        except Exception as e:
            
            logger.error(f"Erro ao pegar pressão para {id} no período {start}, {end}")
            logger.info(f"Info: {str(e)}")
            
        return res
    
    # ---------------------------------------------------------------
    # pressão como pandas
    
    def getPressureAsPandas(self, id, start, end, type='any', module='any'):
        
        res = self.getPressure(id, start, end, type=type, module=module)
        
        if res is not None:
            
            res = pd.DataFrame(res, columns=["sonda", "pressão kPa", "timestamp"])
            
        return res
    
    # ---------------------------------------------------------------
    # get temperature readings
    
    def getTemperature(self, ids, start, end, type='any', module='any'):
        
        res = None

        # Normaliza ids para lista (caso seja um único int)
        try:
            _, probes = to_placeholders(ids)
        except Exception as e:
            logger.error({str(e)})
            return None
        
        if type == 'gas':
            cond_1 = """
                    s.sensor_type = 'gas' AND
                     """
        elif type == 'weather':
            cond_1 = """
                    s.sensor_type = 'weather' AND
                     """
        elif type == 'any':
            cond_1 = None
        else:
            logger.error(f"Tipo de sensor inválido: {type}")
            return None
        
        if module == 'sdc':
            cond_2 = """
                    r.co2_ppm IS NOT NULL AND
                      """  
        elif module == 'bme':
            cond_2 = """
                    r.co2_ppm IS NULL AND
                      """  
        elif module == 'any':
            cond_2 = None
        else:
            logger.error(f"Tipo de módulo inválido {module}")
            return None
        
        cmd = """
                SELECT s.id, r.temperature_c, r.reading_time
                FROM sensor_reading r JOIN sensor s ON s.id = r.sensor_id
                WHERE
                    r.absolute_pressure_kpa IS NOT NULL AND
              """
        cmd_tail = """
                    s.id = ANY(%s) AND
                    r.reading_time >= %s AND
                    r.reading_time <= %s
                ORDER BY reading_time    
              """
              
        
        if cond_1 is not None:
            cmd = cmd + cond_1
            
        if cond_2 is not None:
            cmd = cmd + cond_2
            
        cmd = cmd + cmd_tail
              
        try:
            
            with self.conn.cursor() as cur:
                
                cur.execute(cmd,(probes, start, end,))
                res = cur.fetchall()
                res = [ [ format_datetime(dt) for dt in item ] for item in res ]
                
        except Exception as e:
            
            logger.error(f"Erro ao pegar temperatura para {id} no período {start}, {end}")
            logger.info(f"Info: {str(e)}")
            
        return res
    
    # --------------------------------------------------------------
    # Temperatura como pandas
    
    def getTemperatureAsPandas(self, ids, start, end, type='any', module='any'):
        
        res = self.getTemperature(ids, start, end, type, module)
        
        if res is not None:
            
            res = pd.DataFrame(res, columns=["sonda", "temperatura C", "timestamp"])
            
        return res

    # ---------------------------------------------------------------
    # get humidity readings
    
    def getHumidity(self, ids, start, end, type='any', module='any'):
        
        res = None
        
        # Normaliza ids para lista (caso seja um único int)
        try:
            _, probes = to_placeholders(ids)
        except Exception as e:
            logger.error({str(e)})
            return None
        
        if type == 'gas':
            cond_1 = """
                    s.sensor_type = 'gas' AND
                     """
        elif type == 'weather':
            cond_1 = """
                    s.sensor_type = 'weather' AND
                     """
        elif type == 'any':
            cond_1 = None
        else:
            logger.error(f"Tipo de sensor inválido: {type}")
            return None
        
        if module == 'sdc':
            cond_2 = """
                    r.co2_ppm IS NOT NULL AND
                      """  
        elif module == 'bme':
            cond_2 = """
                    r.co2_ppm IS NULL AND
                      """  
        elif module == 'any':
            cond_2 = None
        else:
            logger.error(f"Tipo de módulo inválido {module}")
            return None
        
        cmd = """
                SELECT s.id, r.humidity_percent, r.reading_time
                FROM sensor_reading r JOIN sensor s ON s.id = r.sensor_id
                WHERE
                    r.humidity_percent IS NOT NULL AND
              """
        cmd_tail = """
                    s.id = ANY(%s) AND
                    r.reading_time >= %s AND
                    r.reading_time <= %s
                ORDER BY r.reading_time
              """
              
        
        if cond_1 is not None:
            cmd = cmd + cond_1
            
        if cond_2 is not None:
            cmd = cmd + cond_2
            
        cmd = cmd + cmd_tail
        
        try:
            
            with self.conn.cursor() as cur:
                
                cur.execute(cmd,(probes, start, end,))
                res = cur.fetchall()
                res = [ [ format_datetime(dt) for dt in item ] for item in res ]
                
        except Exception as e:
            
            logger.error(f"Erro ao pegar umidade para {id} no período {start}, {end}")
            logger.info(f"Info: {str(e)}")
            
        return res
    
    # -----------------------------------------------------
    # Reeturn humidity as pandas
    
    def getHumidityAsPandas(self, ids, start, end, type='any', module='any'):
        
        res = self.getHumidity(ids, start, end, type, module)
        
        if res is not None:
            
            res = pd.DataFrame(res, columns=["sonda", "umidade %", "timestamp"])
            
        return res
    
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
