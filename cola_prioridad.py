"""
Sistema de Cola de Prioridad para Gestión de Camas Hospitalarias.
Implementa una cola de prioridad global por hospital usando max-heap.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Dict, Set, TYPE_CHECKING
import heapq

if TYPE_CHECKING:
    from models import Paciente

# Importar solo el enum desde models (NO importar de main.py ni logic.py)
from models import TipoPacienteEnum


# ============================================
# CONSTANTES DE PRIORIDAD
# ============================================

# Scores base por tipo de paciente
SCORES_TIPO_PACIENTE = {
    TipoPacienteEnum.HOSPITALIZADO: 10,  # Máxima prioridad - ya está en el hospital
    TipoPacienteEnum.URGENCIA: 8,
    TipoPacienteEnum.DERIVADO: 6,
    TipoPacienteEnum.AMBULATORIO: 4,
}

# Scores por complejidad
SCORES_COMPLEJIDAD = {
    "alta": 3,   # UCI
    "media": 2,  # UTI
    "baja": 1,   # Sala básica
}

# Umbrales de espera en horas para penalización
UMBRALES_ESPERA = {
    TipoPacienteEnum.HOSPITALIZADO: 2,   # 2 horas
    TipoPacienteEnum.URGENCIA: 4,        # 4 horas
    TipoPacienteEnum.DERIVADO: 12,       # 12 horas
    TipoPacienteEnum.AMBULATORIO: 48,    # 48 horas
}

# Boosts adicionales
BOOST_EMBARAZADA = 10
BOOST_EDAD_VULNERABLE = 5  # Niños y adultos mayores
BOOST_AISLAMIENTO_INDIVIDUAL = 3
BOOST_DERIVADO_CON_OCUPACION = 4
BOOST_ADULTO_ESPERA_LARGA = 5  # Adulto con más de 8h de espera


# ============================================
# DATACLASS PARA ENTRADA EN COLA
# ============================================

@dataclass(order=True)
class EntradaCola:
    """
    Entrada en la cola de prioridad.
    Usa prioridad negativa para convertir min-heap en max-heap.
    """
    prioridad_negativa: float = field(compare=True)  # Negativo para max-heap
    timestamp: datetime = field(compare=True)  # Desempate por tiempo
    contador: int = field(compare=True)  # Desempate FIFO
    paciente_id: str = field(compare=False)
    hospital_id: str = field(compare=False)


# ============================================
# FUNCIONES DE CÁLCULO DE PRIORIDAD
# ============================================

def calcular_prioridad_paciente(paciente: "Paciente") -> float:
    """
    Calcula la prioridad de un paciente.
    Mayor valor = Mayor prioridad.
    
    Fórmula:
    prioridad = (tipo_base * 10) + (complejidad * 3) + (tiempo_espera * 2) + sum(boosts)
    """
    # 1. Score base por tipo de paciente
    tipo_paciente = getattr(paciente, 'tipo_paciente', None)
    if tipo_paciente is None:
        # Inferir tipo
        if paciente.cama_id:
            tipo_paciente = TipoPacienteEnum.HOSPITALIZADO
        elif getattr(paciente, 'derivacion_pendiente', False):
            tipo_paciente = TipoPacienteEnum.DERIVADO
        else:
            tipo_paciente = TipoPacienteEnum.URGENCIA
    
    score_tipo = SCORES_TIPO_PACIENTE.get(tipo_paciente, 5)
    
    # 2. Score por complejidad
    complejidad = getattr(paciente, 'complejidad_requerida', None)
    if complejidad:
        complejidad_valor = complejidad.value if hasattr(complejidad, 'value') else str(complejidad)
        score_complejidad = SCORES_COMPLEJIDAD.get(complejidad_valor, 1)
    else:
        score_complejidad = 1
    
    # 3. Score por tiempo de espera
    tiempo_espera_min = getattr(paciente, 'tiempo_espera_min', 0) or 0
    tiempo_espera_horas = tiempo_espera_min / 60
    
    # Penalización exponencial si supera umbral
    umbral = UMBRALES_ESPERA.get(tipo_paciente, 12)
    if tiempo_espera_horas > umbral:
        factor_exceso = (tiempo_espera_horas - umbral) / umbral
        score_tiempo = tiempo_espera_horas * (1 + factor_exceso * 0.5)
    else:
        score_tiempo = tiempo_espera_horas
    
    # 4. Boosts adicionales
    boosts = 0
    
    # Embarazada
    if getattr(paciente, 'es_embarazada', False):
        boosts += BOOST_EMBARAZADA
    
    # Edad vulnerable (niños y adultos mayores)
    edad_categoria = getattr(paciente, 'edad_categoria', None)
    if edad_categoria:
        categoria_valor = edad_categoria.value if hasattr(edad_categoria, 'value') else str(edad_categoria)
        if categoria_valor in ['niño', 'lactante', 'adulto_mayor']:
            boosts += BOOST_EDAD_VULNERABLE
    
    # Es adulto mayor explícitamente
    if getattr(paciente, 'es_adulto_mayor', False):
        boosts += BOOST_EDAD_VULNERABLE
    
    # Requiere aislamiento individual
    aislamiento = getattr(paciente, 'aislamiento', None)
    if aislamiento:
        aislamiento_valor = aislamiento.value if hasattr(aislamiento, 'value') else str(aislamiento)
        if aislamiento_valor in ['aereo', 'ambiente_protegido', 'aislamiento_especial']:
            boosts += BOOST_AISLAMIENTO_INDIVIDUAL
    
    # Derivado con ocupación alta en origen
    if tipo_paciente == TipoPacienteEnum.DERIVADO:
        boosts += BOOST_DERIVADO_CON_OCUPACION
    
    # Adulto con espera larga (más de 8 horas)
    if tiempo_espera_horas > 8 and edad_categoria:
        categoria_valor = edad_categoria.value if hasattr(edad_categoria, 'value') else str(edad_categoria)
        if categoria_valor == 'adulto':
            boosts += BOOST_ADULTO_ESPERA_LARGA
    
    # Calcular prioridad final
    prioridad = (score_tipo * 10) + (score_complejidad * 3) + (score_tiempo * 2) + boosts
    
    return round(prioridad, 2)


def explicar_prioridad(paciente: "Paciente") -> Dict:
    """
    Retorna un desglose detallado del cálculo de prioridad.
    Útil para debugging y UI.
    """
    tipo_paciente = getattr(paciente, 'tipo_paciente', None)
    if tipo_paciente is None:
        if paciente.cama_id:
            tipo_paciente = TipoPacienteEnum.HOSPITALIZADO
        elif getattr(paciente, 'derivacion_pendiente', False):
            tipo_paciente = TipoPacienteEnum.DERIVADO
        else:
            tipo_paciente = TipoPacienteEnum.URGENCIA
    
    score_tipo = SCORES_TIPO_PACIENTE.get(tipo_paciente, 5)
    
    complejidad = getattr(paciente, 'complejidad_requerida', None)
    complejidad_valor = complejidad.value if complejidad and hasattr(complejidad, 'value') else 'baja'
    score_complejidad = SCORES_COMPLEJIDAD.get(complejidad_valor, 1)
    
    tiempo_espera_min = getattr(paciente, 'tiempo_espera_min', 0) or 0
    tiempo_espera_horas = tiempo_espera_min / 60
    
    umbral = UMBRALES_ESPERA.get(tipo_paciente, 12)
    if tiempo_espera_horas > umbral:
        factor_exceso = (tiempo_espera_horas - umbral) / umbral
        score_tiempo = tiempo_espera_horas * (1 + factor_exceso * 0.5)
    else:
        score_tiempo = tiempo_espera_horas
    
    boosts_detalle = []
    boosts_total = 0
    
    if getattr(paciente, 'es_embarazada', False):
        boosts_detalle.append({"razon": "Embarazada", "valor": BOOST_EMBARAZADA})
        boosts_total += BOOST_EMBARAZADA
    
    edad_categoria = getattr(paciente, 'edad_categoria', None)
    if edad_categoria:
        categoria_valor = edad_categoria.value if hasattr(edad_categoria, 'value') else str(edad_categoria)
        if categoria_valor in ['niño', 'lactante', 'adulto_mayor']:
            boosts_detalle.append({"razon": f"Edad vulnerable ({categoria_valor})", "valor": BOOST_EDAD_VULNERABLE})
            boosts_total += BOOST_EDAD_VULNERABLE
    
    if getattr(paciente, 'es_adulto_mayor', False) and not any(b['razon'].startswith('Edad') for b in boosts_detalle):
        boosts_detalle.append({"razon": "Adulto mayor", "valor": BOOST_EDAD_VULNERABLE})
        boosts_total += BOOST_EDAD_VULNERABLE
    
    aislamiento = getattr(paciente, 'aislamiento', None)
    if aislamiento:
        aislamiento_valor = aislamiento.value if hasattr(aislamiento, 'value') else str(aislamiento)
        if aislamiento_valor in ['aereo', 'ambiente_protegido', 'aislamiento_especial']:
            boosts_detalle.append({"razon": f"Aislamiento individual ({aislamiento_valor})", "valor": BOOST_AISLAMIENTO_INDIVIDUAL})
            boosts_total += BOOST_AISLAMIENTO_INDIVIDUAL
    
    if tipo_paciente == TipoPacienteEnum.DERIVADO:
        boosts_detalle.append({"razon": "Paciente derivado", "valor": BOOST_DERIVADO_CON_OCUPACION})
        boosts_total += BOOST_DERIVADO_CON_OCUPACION
    
    if tiempo_espera_horas > 8 and edad_categoria:
        categoria_valor = edad_categoria.value if hasattr(edad_categoria, 'value') else str(edad_categoria)
        if categoria_valor == 'adulto':
            boosts_detalle.append({"razon": "Espera larga (>8h)", "valor": BOOST_ADULTO_ESPERA_LARGA})
            boosts_total += BOOST_ADULTO_ESPERA_LARGA
    
    prioridad_total = (score_tipo * 10) + (score_complejidad * 3) + (score_tiempo * 2) + boosts_total
    
    return {
        "prioridad_total": round(prioridad_total, 2),
        "desglose": {
            "tipo_paciente": {
                "tipo": tipo_paciente.value if hasattr(tipo_paciente, 'value') else str(tipo_paciente),
                "score_base": score_tipo,
                "score_total": score_tipo * 10
            },
            "complejidad": {
                "nivel": complejidad_valor,
                "score_base": score_complejidad,
                "score_total": score_complejidad * 3
            },
            "tiempo_espera": {
                "horas": round(tiempo_espera_horas, 2),
                "umbral": umbral,
                "supera_umbral": tiempo_espera_horas > umbral,
                "score_total": round(score_tiempo * 2, 2)
            },
            "boosts": {
                "total": boosts_total,
                "detalles": boosts_detalle
            }
        }
    }


# ============================================
# GESTOR DE COLA DE PRIORIDAD POR HOSPITAL
# ============================================

class GestorColaPrioridad:
    """
    Gestor de cola de prioridad para un hospital específico.
    Usa max-heap con tracking set para eficiencia.
    """
    
    def __init__(self, hospital_id: str):
        self.hospital_id = hospital_id
        self._heap: List[EntradaCola] = []
        self._pacientes_en_cola: Set[str] = set()  # Set para verificación O(1)
        self._contador = 0  # Contador para desempate FIFO
    
    def agregar_paciente(self, paciente: "Paciente", session=None) -> float:
        """
        Agrega un paciente a la cola de prioridad.
        Previene duplicados usando el set de tracking.
        """
        if paciente.id in self._pacientes_en_cola:
            print(f"⚠️ Paciente {paciente.nombre} ya está en la cola")
            return 0
        
        # Calcular prioridad
        prioridad = calcular_prioridad_paciente(paciente)
        
        # Crear entrada
        entrada = EntradaCola(
            prioridad_negativa=-prioridad,  # Negativo para max-heap
            timestamp=datetime.utcnow(),
            contador=self._contador,
            paciente_id=paciente.id,
            hospital_id=self.hospital_id
        )
        
        self._contador += 1
        
        # Agregar al heap y al set
        heapq.heappush(self._heap, entrada)
        self._pacientes_en_cola.add(paciente.id)
        
        # Actualizar paciente en BD si se proporciona session
        if session:
            paciente.en_lista_espera = True
            paciente.prioridad_calculada = prioridad
            session.add(paciente)
        
        print(f"✅ Paciente {paciente.nombre} agregado a cola con prioridad {prioridad}")
        return prioridad
    
    def actualizar_prioridad(self, paciente: "Paciente", session=None) -> float:
        """
        Actualiza la prioridad de un paciente.
        Si no está en la cola, lo agrega.
        """
        if paciente.id in self._pacientes_en_cola:
            # Remover y re-agregar (más simple que buscar y modificar)
            self.remover_paciente(paciente.id, session, paciente)
        
        return self.agregar_paciente(paciente, session)
    
    def obtener_siguiente(self) -> Optional[str]:
        """
        Obtiene el ID del siguiente paciente más prioritario SIN removerlo.
        Salta entradas de pacientes ya removidos.
        """
        while self._heap:
            entrada = self._heap[0]
            
            if entrada.paciente_id in self._pacientes_en_cola:
                return entrada.paciente_id
            else:
                heapq.heappop(self._heap)
        
        return None
    
    def remover_paciente(self, paciente_id: str, session=None, paciente: "Paciente" = None) -> bool:
        """
        Remueve un paciente de la cola.
        Usa el set de tracking para verificación rápida.
        """
        if paciente_id not in self._pacientes_en_cola:
            print(f"⚠️ Paciente {paciente_id} no está en la cola")
            return False
        
        self._pacientes_en_cola.discard(paciente_id)
        
        if session and paciente:
            paciente.en_lista_espera = False
            paciente.prioridad_calculada = 0
            session.add(paciente)
        
        print(f"✅ Paciente {paciente_id} removido de cola. Quedan: {len(self._pacientes_en_cola)}")
        return True
    
    def eliminar_paciente(self, paciente_id: str, session=None, paciente: "Paciente" = None) -> bool:
        """Alias de remover_paciente para compatibilidad."""
        return self.remover_paciente(paciente_id, session, paciente)
    
    def pop_siguiente(self) -> Optional[str]:
        """
        Obtiene Y REMUEVE el siguiente paciente más prioritario.
        Limpia entradas obsoletas y actualiza tracking.
        """
        while self._heap:
            entrada = heapq.heappop(self._heap)
            
            if entrada.paciente_id in self._pacientes_en_cola:
                self._pacientes_en_cola.discard(entrada.paciente_id)
                return entrada.paciente_id
        
        return None
    
    def esta_en_cola(self, paciente_id: str) -> bool:
        """Verifica si un paciente está en la cola."""
        return paciente_id in self._pacientes_en_cola
    
    def esta_vacio(self) -> bool:
        """Verifica si la cola está vacía."""
        return len(self._pacientes_en_cola) == 0
    
    def obtener_lista_ordenada(self, session=None) -> List[Dict]:
        """
        Retorna la lista de pacientes ordenados por prioridad.
        Solo incluye pacientes que están en el set de tracking.
        """
        entradas_validas = [e for e in self._heap if e.paciente_id in self._pacientes_en_cola]
        entradas_ordenadas = sorted(entradas_validas, key=lambda x: x.prioridad_negativa)
        
        resultado = []
        for entrada in entradas_ordenadas:
            info = {
                "paciente_id": entrada.paciente_id,
                "prioridad": -entrada.prioridad_negativa,
                "timestamp": entrada.timestamp.isoformat(),
                "posicion": len(resultado) + 1
            }
            
            if session:
                from sqlmodel import select
                from models import Paciente
                paciente = session.get(Paciente, entrada.paciente_id)
                if paciente:
                    info.update({
                        "nombre": paciente.nombre,
                        "tipo_paciente": paciente.tipo_paciente.value if paciente.tipo_paciente else "desconocido",
                        "complejidad": paciente.complejidad_requerida.value,
                        "tiempo_espera_min": paciente.tiempo_espera_min,
                        "tiene_cama_actual": paciente.cama_id is not None,
                        "cama_actual_id": paciente.cama_id
                    })
            
            resultado.append(info)
        
        return resultado
    
    def limpiar_cola(self):
        """Limpia completamente la cola."""
        self._heap = []
        self._pacientes_en_cola = set()
        self._contador = 0
        print(f"🧹 Cola del hospital {self.hospital_id} limpiada")
    
    def tamano(self) -> int:
        """Retorna el número de pacientes en la cola."""
        return len(self._pacientes_en_cola)
    
    def __len__(self) -> int:
        """Retorna el número de pacientes en la cola."""
        return len(self._pacientes_en_cola)


# ============================================
# GESTOR GLOBAL DE COLAS
# ============================================

class GestorColasGlobal:
    """
    Singleton que mantiene las colas de prioridad de todos los hospitales.
    """
    _instance = None
    _colas: Dict[str, GestorColaPrioridad] = {}
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._colas = {}
        return cls._instance
    
    def obtener_cola(self, hospital_id: str) -> GestorColaPrioridad:
        """Obtiene o crea la cola de prioridad para un hospital."""
        if hospital_id not in self._colas:
            self._colas[hospital_id] = GestorColaPrioridad(hospital_id)
            print(f"📋 Cola de prioridad creada para hospital {hospital_id}")
        return self._colas[hospital_id]
    
    def agregar_paciente(self, paciente: "Paciente", hospital_id: str, session=None) -> float:
        """
        Agrega un paciente a la cola de su hospital.
        Método de conveniencia.
        """
        cola = self.obtener_cola(hospital_id)
        return cola.agregar_paciente(paciente, session)
    
    def remover_paciente(self, paciente_id: str, hospital_id: str, session=None, paciente=None) -> bool:
        """
        Remueve un paciente de la cola de su hospital.
        Método de conveniencia.
        """
        cola = self.obtener_cola(hospital_id)
        return cola.remover_paciente(paciente_id, session, paciente)
    
    def eliminar_paciente(self, paciente_id: str, hospital_id: str, session=None, paciente=None) -> bool:
        """
        Elimina un paciente de la cola de su hospital.
        Alias para remover_paciente para compatibilidad.
        """
        cola = self.obtener_cola(hospital_id)
        return cola.remover_paciente(paciente_id, session, paciente)
    
    def sincronizar_cola_con_db(self, hospital_id: str, session) -> int:
        """
        Sincroniza la cola con el estado actual de la base de datos.
        Útil para recuperación después de un reinicio.
        """
        from sqlmodel import select
        from models import Paciente
        
        cola = self.obtener_cola(hospital_id)
        cola.limpiar_cola()
        
        # Buscar pacientes que están en lista de espera
        query = select(Paciente).where(
            Paciente.hospital_id == hospital_id
        )
        todos_pacientes = session.exec(query).all()
        
        # Filtrar pacientes que necesitan estar en la cola
        pacientes_para_cola = []
        for paciente in todos_pacientes:
            # Debe estar en cola si:
            # 1. Tiene en_lista_espera = True
            # 2. O tiene en_espera = True y no tiene cama_destino
            # 3. O requiere_nueva_cama = True y no tiene cama_destino
            necesita_cola = (
                paciente.en_lista_espera or
                (paciente.en_espera and not paciente.cama_destino_id) or
                (getattr(paciente, 'requiere_nueva_cama', False) and not paciente.cama_destino_id)
            )
            
            if necesita_cola:
                pacientes_para_cola.append(paciente)
        
        # Agregar a la cola
        for paciente in pacientes_para_cola:
            cola.agregar_paciente(paciente, session)
        
        session.commit()
        
        print(f"🔄 Cola sincronizada para {hospital_id}: {len(cola)} pacientes")
        return len(cola)
    
    def limpiar_todas(self):
        """Limpia todas las colas."""
        for hospital_id, cola in self._colas.items():
            cola.limpiar_cola()
        print("🧹 Todas las colas limpiadas")


# Instancia global
gestor_colas_global = GestorColasGlobal()