"""
LOGIC.PY - Lógica de Negocio para Gestión de Camas Hospitalarias

Este módulo contiene la lógica de:
- Determinación de servicio requerido
- Asignación de camas
- Filtrado y priorización de camas
- Gestión de salas compartidas
- Integración con cola de prioridad

IMPORTANTE: Este archivo NO define modelos. Todos los modelos se importan desde models.py
"""

from typing import List, Optional, Tuple, Dict

# ============================================
# IMPORTACIONES DESDE MODELS.PY
# ============================================
# Importar SOLO lo necesario desde models.py
# NO redefinir clases aquí

from models import (
    Paciente, 
    Cama,
    ServicioEnum, 
    EnfermedadEnum, 
    AislamientoEnum,
    ComplejidadEnum, 
    EstadoCamaEnum, 
    TipoPacienteEnum,
    EdadCategoriaEnum, 
    SexoEnum,
    calcular_puntos_complejidad, 
    determinar_complejidad_por_puntos,
    tiene_requerimientos_uci, 
    tiene_requerimientos_uti,
    REQUERIMIENTOS_SIN_HOSPITALIZACION
)


# ============================================
# LÓGICA DE COMPLEJIDAD Y SERVICIO
# ============================================

def actualizar_complejidad_paciente(paciente: Paciente) -> ComplejidadEnum:
    """
    Calcula y actualiza la complejidad requerida por el paciente según sus requerimientos.
    """
    puntos = calcular_puntos_complejidad(paciente.requerimientos)
    paciente.puntos_complejidad = puntos
    complejidad = determinar_complejidad_por_puntos(puntos)
    paciente.complejidad_requerida = complejidad
    return complejidad


def requiere_aislamiento_individual(aislamiento: AislamientoEnum) -> bool:
    """Determina si el tipo de aislamiento requiere cama individual."""
    return aislamiento in [
        AislamientoEnum.AEREO, 
        AislamientoEnum.AMBIENTE_PROTEGIDO, 
        AislamientoEnum.AISLAMIENTO_ESPECIAL
    ]


def puede_compartir_aislamiento(aislamiento: AislamientoEnum) -> bool:
    """Determina si el tipo de aislamiento puede compartir sala."""
    return aislamiento in [AislamientoEnum.CONTACTO, AislamientoEnum.GOTITAS]


def determinar_servicio_requerido(paciente: Paciente, camas_disponibles=None) -> Optional[ServicioEnum]:
    """
    Determina el servicio hospitalario requerido para el paciente.
    Retorna None si el paciente puede ser dado de alta.
    """
    
    # PRIORIDAD 1: Verificar casos especiales
    if paciente.caso_sociosanitario or paciente.espera_cardio:
        if tiene_requerimientos_uci(paciente.requerimientos):
            return ServicioEnum.UCI
        if tiene_requerimientos_uti(paciente.requerimientos):
            return ServicioEnum.UTI
        return ServicioEnum.MEDICINA
    
    # PRIORIDAD 2: Verificar si debe dar de alta
    if len(paciente.requerimientos) == 0:
        return None
    
    requerimientos_validos = [req for req in paciente.requerimientos 
                              if req not in REQUERIMIENTOS_SIN_HOSPITALIZACION]
    if len(requerimientos_validos) == 0:
        return None
    
    # PRIORIDAD 3: UCI por requerimientos específicos
    if tiene_requerimientos_uci(paciente.requerimientos):
        return ServicioEnum.UCI
    
    # PRIORIDAD 4: UTI por requerimientos específicos
    if tiene_requerimientos_uti(paciente.requerimientos):
        return ServicioEnum.UTI
    
    # PRIORIDAD 5: Aislamiento individual SIN criterios UCI/UTI
    if requiere_aislamiento_individual(paciente.aislamiento):
        if not tiene_requerimientos_uci(paciente.requerimientos) and not tiene_requerimientos_uti(paciente.requerimientos):
            return ServicioEnum.AISLAMIENTO
    
    # PRIORIDAD 6: Aislamiento especial
    if paciente.aislamiento == AislamientoEnum.AISLAMIENTO_ESPECIAL:
        return ServicioEnum.AISLAMIENTO
    
    # PRIORIDAD 7: Tipo de enfermedad
    if paciente.enfermedad == EnfermedadEnum.QUIRURGICA:
        return ServicioEnum.CIRUGIA
    elif paciente.enfermedad == EnfermedadEnum.GINECOLOGICA or paciente.enfermedad == EnfermedadEnum.OBSTETRICA:
        return ServicioEnum.GINECO
    else:
        return ServicioEnum.MEDICINA


def puede_asignar_sala_compartida(cama: Cama, paciente: Paciente, camas_misma_sala: List[Cama]) -> bool:
    """
    Verifica si un paciente puede ser asignado a una cama en sala compartida.
    """
    if not cama.es_sala_compartida:
        return True
    
    # Contar pacientes actuales en la sala
    pacientes_en_sala = sum(1 for c in camas_misma_sala 
                            if c.sala == cama.sala 
                            and c.estado in [EstadoCamaEnum.OCUPADA, EstadoCamaEnum.PENDIENTE_TRASLADO])
    
    if pacientes_en_sala >= cama.capacidad_sala:
        return False
    
    if pacientes_en_sala == 0:
        return True
    
    if cama.sexo_sala is None:
        return False
    
    return cama.sexo_sala == paciente.sexo


# ============================================
# LÓGICA DE FILTRADO DE CAMAS
# ============================================

def descartar_salas_sexo_incompatible(
    camas_libres: List[Cama],
    todas_camas: List[Cama],
    paciente: Paciente
) -> List[Cama]:
    """
    Elimina camas de salas con sexo incompatible.
    """
    if not camas_libres:
        return []
    
    hospital_id = camas_libres[0].hospital_id
    
    salas_info = {}
    for cama in todas_camas:
        if cama.hospital_id != hospital_id:
            continue
            
        sala_id = f"{cama.hospital_id}:{cama.sala}"
        
        if sala_id not in salas_info:
            salas_info[sala_id] = {
                'sala': cama.sala,
                'sexo_sala': cama.sexo_sala,
                'es_compartida': cama.es_sala_compartida,
                'pacientes_actuales': 0,
                'tiene_sexo_opuesto': False
            }
        
        if cama.estado in [EstadoCamaEnum.OCUPADA, EstadoCamaEnum.PENDIENTE_TRASLADO]:
            salas_info[sala_id]['pacientes_actuales'] += 1
            if cama.sexo_sala and cama.sexo_sala != paciente.sexo:
                salas_info[sala_id]['tiene_sexo_opuesto'] = True
    
    camas_compatibles = []
    
    for cama in camas_libres:
        sala_id = f"{cama.hospital_id}:{cama.sala}"
        
        if not cama.es_sala_compartida:
            camas_compatibles.append(cama)
            continue
        
        info_sala = salas_info.get(sala_id, {'pacientes_actuales': 0, 'tiene_sexo_opuesto': False})
        
        if info_sala['pacientes_actuales'] == 0:
            camas_compatibles.append(cama)
            continue
        
        if info_sala['tiene_sexo_opuesto']:
            continue
        
        camas_compatibles.append(cama)
    
    return camas_compatibles


def filtrar_por_servicio(camas: List[Cama], servicio: ServicioEnum) -> List[Cama]:
    """Filtra camas por servicio con soporte para MEDICO_QUIRURGICO."""
    if servicio in [ServicioEnum.MEDICINA, ServicioEnum.CIRUGIA]:
        return [c for c in camas if c.servicio == servicio or c.servicio == ServicioEnum.MEDICO_QUIRURGICO]
    else:
        return [c for c in camas if c.servicio == servicio]


def filtrar_por_aislamiento(camas: List[Cama], paciente: Paciente) -> List[Cama]:
    """Filtra camas según requerimientos de aislamiento."""
    
    if paciente.aislamiento == AislamientoEnum.NINGUNO:
        return camas
    
    if puede_compartir_aislamiento(paciente.aislamiento):
        camas_aislamiento = [c for c in camas if c.permite_aislamiento_compartido]
        if camas_aislamiento:
            return camas_aislamiento
        return camas
    
    if requiere_aislamiento_individual(paciente.aislamiento):
        tiene_uci = tiene_requerimientos_uci(paciente.requerimientos)
        tiene_uti = tiene_requerimientos_uti(paciente.requerimientos)
        
        if tiene_uci or tiene_uti:
            return [c for c in camas if c.es_cama_individual]
        else:
            return [c for c in camas if c.es_cama_individual and c.servicio == ServicioEnum.AISLAMIENTO]
    
    return camas


def priorizar_camas(camas: List[Cama], paciente: Paciente) -> List[Cama]:
    """Prioriza camas según criterios de asignación óptima."""
    
    def calcular_prioridad(cama: Cama) -> Tuple[int, int, int, int]:
        servicio_requerido = determinar_servicio_requerido(paciente)
        
        if cama.servicio == servicio_requerido:
            prioridad_servicio = 0
        elif cama.servicio == ServicioEnum.MEDICO_QUIRURGICO and servicio_requerido in [ServicioEnum.MEDICINA, ServicioEnum.CIRUGIA]:
            prioridad_servicio = 1
        else:
            prioridad_servicio = 2
        
        if cama.complejidad == paciente.complejidad_requerida:
            prioridad_complejidad = 0
        else:
            prioridad_complejidad = abs(
                ["baja", "media", "alta"].index(cama.complejidad.value) -
                ["baja", "media", "alta"].index(paciente.complejidad_requerida.value)
            )
        
        if requiere_aislamiento_individual(paciente.aislamiento):
            prioridad_tipo = 0 if cama.es_cama_individual else 1
        else:
            prioridad_tipo = 0 if cama.es_sala_compartida else 1
        
        prioridad_sala = cama.numero
        
        return (prioridad_servicio, prioridad_complejidad, prioridad_tipo, prioridad_sala)
    
    return sorted(camas, key=calcular_prioridad)


# ============================================
# FUNCIÓN PRINCIPAL DE ASIGNACIÓN
# ============================================

def buscar_cama_para_paciente(
    paciente: Paciente,
    camas_disponibles: List[Cama],
    todas_camas: List[Cama]
) -> Optional[Cama]:
    """
    Busca la mejor cama disponible para un paciente.
    NO modifica el estado de la cama ni del paciente.
    
    Args:
        paciente: Paciente que necesita cama
        camas_disponibles: Lista de camas libres
        todas_camas: Lista de todas las camas del hospital
        
    Returns:
        Mejor cama disponible o None si no hay ninguna compatible
    """
    if not camas_disponibles:
        return None
    
    # 1. Filtrar por compatibilidad de sexo
    camas_compatibles_sexo = descartar_salas_sexo_incompatible(
        camas_disponibles, 
        todas_camas, 
        paciente
    )
    
    if not camas_compatibles_sexo:
        return None
    
    # 2. Determinar servicio requerido
    servicio_requerido = determinar_servicio_requerido(paciente)
    
    if servicio_requerido is None:
        return None
    
    # 3. Filtrar por servicio
    camas_servicio = filtrar_por_servicio(camas_compatibles_sexo, servicio_requerido)
    
    if not camas_servicio:
        return None
    
    # 4. Filtrar por aislamiento
    camas_aislamiento = filtrar_por_aislamiento(camas_servicio, paciente)
    
    if not camas_aislamiento:
        return None
    
    # 5. Priorizar camas
    camas_priorizadas = priorizar_camas(camas_aislamiento, paciente)
    
    # 6. Verificar disponibilidad real
    for cama in camas_priorizadas:
        camas_misma_sala = [c for c in todas_camas if c.sala == cama.sala and c.hospital_id == cama.hospital_id]
        if puede_asignar_sala_compartida(cama, paciente, camas_misma_sala):
            return cama
    
    return None


def asignar_cama(
    paciente: Paciente,
    camas_disponibles: List[Cama],
    todas_camas: List[Cama],
    session,
    solo_verificar: bool = False
) -> Optional[Cama]:
    """
    Función legacy para compatibilidad.
    Usa buscar_cama_para_paciente internamente.
    """
    return buscar_cama_para_paciente(paciente, camas_disponibles, todas_camas)


def buscar_candidatos_cama(
    paciente: Paciente, 
    camas_disponibles: List[Cama],
    todas_camas: List[Cama],
    limite: int = 3
) -> List[Cama]:
    """
    Busca y devuelve una lista de las mejores camas candidatas para un paciente.
    """
    if not camas_disponibles:
        return []
    
    camas_compatibles_sexo = descartar_salas_sexo_incompatible(
        camas_disponibles, 
        todas_camas, 
        paciente
    )
    
    if not camas_compatibles_sexo:
        return []
    
    servicio_requerido = determinar_servicio_requerido(paciente)
    
    if servicio_requerido is None:
        return []
    
    camas_servicio = filtrar_por_servicio(camas_compatibles_sexo, servicio_requerido)
    
    if not camas_servicio:
        return []
    
    camas_aislamiento = filtrar_por_aislamiento(camas_servicio, paciente)
    
    if not camas_aislamiento:
        return []
    
    camas_priorizadas = priorizar_camas(camas_aislamiento, paciente)
    
    candidatos_finales = []
    for cama in camas_priorizadas:
        camas_misma_sala = [c for c in todas_camas if c.sala == cama.sala and c.hospital_id == cama.hospital_id]
        if puede_asignar_sala_compartida(cama, paciente, camas_misma_sala):
            candidatos_finales.append(cama)
            if len(candidatos_finales) >= limite:
                break
                
    return candidatos_finales


# ============================================
# LÓGICA DE VERIFICACIÓN DE CAMBIOS
# ============================================

def requiere_cambio_cama(paciente: Paciente, cama_actual: Cama, camas_disponibles=None) -> bool:
    """
    Determina si un paciente requiere cambio de cama basándose en sus requerimientos actuales.
    """
    
    if not cama_actual:
        return False
    
    # Verificar aislamiento individual
    if requiere_aislamiento_individual(paciente.aislamiento):
        if cama_actual.es_cama_individual:
            if cama_actual.servicio in [ServicioEnum.UCI, ServicioEnum.UTI]:
                if cama_actual.servicio == ServicioEnum.UCI and tiene_requerimientos_uci(paciente.requerimientos):
                    return False
                elif cama_actual.servicio == ServicioEnum.UTI and tiene_requerimientos_uti(paciente.requerimientos):
                    return False
                else:
                    return True
            
            if cama_actual.servicio == ServicioEnum.AISLAMIENTO:
                if tiene_requerimientos_uci(paciente.requerimientos) or tiene_requerimientos_uti(paciente.requerimientos):
                    return True
                return False
        else:
            return True
    
    # Verificar cambio de servicio
    servicio_requerido = determinar_servicio_requerido(paciente, camas_disponibles)
    servicio_actual = cama_actual.servicio
    
    if servicio_requerido is None:
        return False
    
    if servicio_actual == ServicioEnum.MEDICO_QUIRURGICO:
        if servicio_requerido not in [ServicioEnum.MEDICINA, ServicioEnum.CIRUGIA, ServicioEnum.MEDICO_QUIRURGICO]:
            return True
    elif servicio_requerido != servicio_actual:
        return True
    
    return False


def requiere_alta(paciente: Paciente) -> bool:
    """Determina si un paciente debe ser dado de alta."""
    
    if paciente.caso_sociosanitario or paciente.espera_cardio:
        return False
    
    if len(paciente.requerimientos) == 0:
        return True
    
    requerimientos_validos = [req for req in paciente.requerimientos 
                              if req not in REQUERIMIENTOS_SIN_HOSPITALIZACION]
    
    if len(requerimientos_validos) == 0:
        return True
    
    return False


# ============================================
# GESTIÓN DE SALAS COMPARTIDAS
# ============================================

def actualizar_sexo_sala(cama: Cama, paciente: Paciente, todas_camas: List[Cama], session):
    """Actualiza el sexo de la sala cuando se asigna un paciente."""
    if not cama.es_sala_compartida:
        return
    
    camas_misma_sala = [c for c in todas_camas if c.sala == cama.sala and c.hospital_id == cama.hospital_id]
    
    pacientes_en_sala = sum(1 for c in camas_misma_sala 
                           if c.id != cama.id 
                           and c.estado in [EstadoCamaEnum.OCUPADA, EstadoCamaEnum.PENDIENTE_TRASLADO])
    
    if pacientes_en_sala == 0:
        for c in camas_misma_sala:
            c.sexo_sala = paciente.sexo
            c.pacientes_en_sala = 1
            session.add(c)
    else:
        for c in camas_misma_sala:
            c.pacientes_en_sala = pacientes_en_sala + 1
            session.add(c)


def liberar_sexo_sala(cama: Cama, todas_camas: List[Cama], session):
    """Actualiza el sexo de la sala cuando se libera un paciente."""
    if not cama.es_sala_compartida:
        return
    
    camas_misma_sala = [c for c in todas_camas if c.sala == cama.sala and c.hospital_id == cama.hospital_id]
    
    for c in camas_misma_sala:
        c.pacientes_en_sala = max(0, c.pacientes_en_sala - 1)
        if c.pacientes_en_sala == 0:
            c.sexo_sala = None
        session.add(c)


# ============================================
# ESTADÍSTICAS Y REPORTES
# ============================================

def generar_reporte_ocupacion(camas: List[Cama]) -> Dict:
    """Genera un reporte de ocupación de camas."""
    reporte = {
        "total": len(camas),
        "libres": 0,
        "ocupadas": 0,
        "en_traslado": 0,
        "pendiente_traslado": 0,
        "alta_sugerida": 0,
        "por_servicio": {}
    }
    
    for cama in camas:
        if cama.estado == EstadoCamaEnum.LIBRE:
            reporte["libres"] += 1
        elif cama.estado == EstadoCamaEnum.OCUPADA:
            reporte["ocupadas"] += 1
        elif cama.estado == EstadoCamaEnum.EN_TRASLADO:
            reporte["en_traslado"] += 1
        elif cama.estado == EstadoCamaEnum.PENDIENTE_TRASLADO:
            reporte["pendiente_traslado"] += 1
        elif cama.estado == EstadoCamaEnum.ALTA_SUGERIDA:
            reporte["alta_sugerida"] += 1
        
        servicio_key = cama.servicio.value
        if servicio_key not in reporte["por_servicio"]:
            reporte["por_servicio"][servicio_key] = {
                "total": 0,
                "libres": 0,
                "ocupadas": 0
            }
        
        reporte["por_servicio"][servicio_key]["total"] += 1
        if cama.estado == EstadoCamaEnum.LIBRE:
            reporte["por_servicio"][servicio_key]["libres"] += 1
        elif cama.estado == EstadoCamaEnum.OCUPADA:
            reporte["por_servicio"][servicio_key]["ocupadas"] += 1
    
    return reporte


# ============================================
# LÓGICA DE PRIORIZACIÓN DE COLA
# ============================================

def priorizar_pacientes(pacientes_en_espera: List[Paciente]) -> List[Paciente]:
    """
    Ordena la lista de pacientes en espera según criterios de prioridad clínica.
    """
    
    def calcular_prioridad(paciente: Paciente) -> Tuple[int, int, int, int]:
        """
        Calcula una tupla de valores para la ordenación.
        Menor valor = Mayor prioridad
        """
        
        # Prioridad 1: Embarazada (máxima prioridad)
        if paciente.es_embarazada:
            return (0, 0, 0, -paciente.tiempo_espera_min)
        
        # Verificar si hay alguien esperando más de 12 horas (720 minutos)
        hay_espera_larga = any(p.tiempo_espera_min > 720 for p in pacientes_en_espera)
        
        # Prioridad 2: Adulto mayor (solo si no hay esperas >12h)
        if paciente.edad_categoria == EdadCategoriaEnum.ADULTO_MAYOR and not hay_espera_larga:
            return (1, 0, -paciente.puntos_complejidad, -paciente.tiempo_espera_min)
        
        # Prioridad 3: Por puntaje de complejidad
        return (2, 0, -paciente.puntos_complejidad, -paciente.tiempo_espera_min)
    
    # Ordenar por tupla de prioridad
    pacientes_ordenados = sorted(
        pacientes_en_espera,
        key=calcular_prioridad
    )
    
    return pacientes_ordenados


# ============================================
# LÓGICA DE ASIGNACIÓN EN BATCH
# ============================================

def asignar_camas_batch(
    pacientes_en_espera: List[Paciente], 
    camas_disponibles: List[Cama],
    todas_camas: List[Cama],
    session
) -> List[Tuple[Paciente, Optional[Cama]]]:
    """
    Asigna camas a múltiples pacientes respetando prioridades.
    Retorna una lista de tuplas (paciente, cama_asignada).
    """
    # 1. Priorizar pacientes
    pacientes_priorizados = priorizar_pacientes(pacientes_en_espera)
    
    # 2. Asignar camas uno por uno
    asignaciones = []
    
    # Crear diccionario de camas disponibles
    camas_restantes: Dict[str, Cama] = {
        c.id: c for c in camas_disponibles 
        if c.estado == EstadoCamaEnum.LIBRE
    }
    
    for paciente in pacientes_priorizados:
        # Intentar asignar una cama
        cama_asignada = asignar_cama(
            paciente, 
            list(camas_restantes.values()), 
            todas_camas, 
            session
        )
        
        if cama_asignada:
            # Remover la cama de las disponibles
            del camas_restantes[cama_asignada.id]
            asignaciones.append((paciente, cama_asignada))
        else:
            # Si no se asigna, sigue pendiente
            asignaciones.append((paciente, None))
            
    return asignaciones