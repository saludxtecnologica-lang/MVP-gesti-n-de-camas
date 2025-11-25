from typing import List, Optional, Tuple, Dict
from models import (
    Paciente, Cama,
    ServicioEnum, EnfermedadEnum, AislamientoEnum,
    ComplejidadEnum, EstadoCamaEnum,
    EdadCategoriaEnum, SexoEnum,
    calcular_puntos_complejidad, determinar_complejidad_por_puntos,
    tiene_requerimientos_uci, tiene_requerimientos_uti,
    REQUERIMIENTOS_SIN_HOSPITALIZACION
)


# ============================================
# LÓGICA DE COMPLEJIDAD Y SERVICIO
# ============================================

def actualizar_complejidad_paciente(paciente: Paciente) -> ComplejidadEnum:
    """
    Calcula y actualiza la complejidad requerida por el paciente según sus requerimientos.
    NOTA: La complejidad ahora es solo para priorización, no determina el servicio.
    """
    puntos = calcular_puntos_complejidad(paciente.requerimientos)
    paciente.puntos_complejidad = puntos
    complejidad = determinar_complejidad_por_puntos(puntos)
    paciente.complejidad_requerida = complejidad
    return complejidad


def requiere_aislamiento_individual(aislamiento: AislamientoEnum) -> bool:
    """Determina si el tipo de aislamiento requiere cama individual."""
    return aislamiento in [AislamientoEnum.AEREO, AislamientoEnum.AMBIENTE_PROTEGIDO, AislamientoEnum.AISLAMIENTO_ESPECIAL]


def puede_compartir_aislamiento(aislamiento: AislamientoEnum) -> bool:
    """Determina si el tipo de aislamiento puede compartir sala."""
    return aislamiento in [AislamientoEnum.CONTACTO, AislamientoEnum.GOTITAS]


def determinar_servicio_requerido(paciente: Paciente, camas_disponibles=None) -> Optional[ServicioEnum]:
    """
    Determina el servicio hospitalario requerido.
    ✅ CORRECCIÓN: Mantiene hospitalizado con requerimientos especiales
    ✅ CORRECCIÓN: Prioriza AISLAMIENTO si requiere aislamiento individual SIN criterios UCI/UTI
    """
    
    # ✅ PRIORIDAD 1: Verificar casos especiales (SIEMPRE se mantienen hospitalizados)
    if paciente.caso_sociosanitario or paciente.espera_cardio:
        # Los casos especiales van a medicina si no tienen otros requerimientos críticos
        if tiene_requerimientos_uci(paciente.requerimientos):
            return ServicioEnum.UCI
        if tiene_requerimientos_uti(paciente.requerimientos):
            return ServicioEnum.UTI
        # Por defecto van a medicina
        return ServicioEnum.MEDICINA
    
    # ✅ PRIORIDAD 2: Verificar si debe dar de alta (sin casos especiales)
    # Si no tiene requerimientos clínicos Y no tiene casos especiales, puede dar alta
    if len(paciente.requerimientos) == 0:
        return None  # Dar de alta
    
    # Si SOLO tiene requerimientos que NO definen hospitalización
    requerimientos_validos = [req for req in paciente.requerimientos 
                              if req not in REQUERIMIENTOS_SIN_HOSPITALIZACION]
    if len(requerimientos_validos) == 0:
        return None  # Dar de alta
    
    # ✅ PRIORIDAD 3: UCI por requerimientos específicos
    if tiene_requerimientos_uci(paciente.requerimientos):
        return ServicioEnum.UCI
    
    # ✅ PRIORIDAD 4: UTI por requerimientos específicos
    if tiene_requerimientos_uti(paciente.requerimientos):
        return ServicioEnum.UTI
    
    # ✅ PRIORIDAD 5: Aislamiento individual SIN criterios UCI/UTI
    # Si requiere aislamiento individual pero NO tiene criterios UCI/UTI, debe ir a AISLAMIENTO
    if requiere_aislamiento_individual(paciente.aislamiento):
        if not tiene_requerimientos_uci(paciente.requerimientos) and not tiene_requerimientos_uti(paciente.requerimientos):
            print(f"📋 Paciente requiere aislamiento individual SIN criterios UCI/UTI → Servicio AISLAMIENTO")
            return ServicioEnum.AISLAMIENTO
    
    # ✅ PRIORIDAD 6: Aislamiento especial (KPC, C. difficile, etc.)
    # Este tipo específico siempre va al servicio de aislamiento
    if paciente.aislamiento == AislamientoEnum.AISLAMIENTO_ESPECIAL:
        return ServicioEnum.AISLAMIENTO
    
    # ✅ PRIORIDAD 7: Tipo de enfermedad
    if paciente.enfermedad == EnfermedadEnum.QUIRURGICA:
        return ServicioEnum.CIRUGIA
    elif paciente.enfermedad == EnfermedadEnum.GINECOLOGICA or paciente.enfermedad == EnfermedadEnum.OBSTETRICA:
        return ServicioEnum.GINECO
    else:
        # Médica, traumatológica, neurológica, geriátrica, urológica van a medicina
        return ServicioEnum.MEDICINA


def puede_asignar_sala_compartida(cama: Cama, paciente: Paciente, camas_misma_sala: List[Cama]) -> bool:
    """
    Verifica si un paciente puede ser asignado a una cama en sala compartida.
    
    Reglas:
    - Si la sala está vacía (sin pacientes), puede entrar cualquiera
    - Si la sala tiene pacientes, debe ser del mismo sexo
    - La sala no puede exceder su capacidad
    
    CRÍTICO: Cuenta tanto OCUPADA como PENDIENTE_TRASLADO
    """
    if not cama.es_sala_compartida:
        return True  # Camas individuales siempre disponibles
    
    # Contar pacientes actuales en la sala (OCUPADA y PENDIENTE_TRASLADO)
    pacientes_en_sala = sum(1 for c in camas_misma_sala 
                            if c.sala == cama.sala 
                            and c.estado in [EstadoCamaEnum.OCUPADA, EstadoCamaEnum.PENDIENTE_TRASLADO])
    
    # Verificar capacidad
    if pacientes_en_sala >= cama.capacidad_sala:
        return False
    
    # Si la sala está realmente vacía, puede entrar
    if pacientes_en_sala == 0:
        return True
    
    # Si hay pacientes, DEBE verificar sexo
    if cama.sexo_sala is None:
        # Si hay pacientes pero sexo no está establecido, es un error
        # Por seguridad, NO permitir
        print(f"⚠️ ADVERTENCIA: Sala {cama.sala} tiene {pacientes_en_sala} pacientes pero sexo_sala es None")
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
    DESCARTE EXPLÍCITO: Elimina camas de salas con sexo incompatible.
    """
    # Agrupar todas las camas por sala
    salas_info = {}
    
    for cama in todas_camas:
        if cama.hospital_id != camas_libres[0].hospital_id if camas_libres else None:
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
        
        # Contar pacientes en la sala (OCUPADA y PENDIENTE_TRASLADO)
        if cama.estado in [EstadoCamaEnum.OCUPADA, EstadoCamaEnum.PENDIENTE_TRASLADO]:
            salas_info[sala_id]['pacientes_actuales'] += 1
            
            # Verificar si tiene sexo opuesto
            if cama.sexo_sala and cama.sexo_sala != paciente.sexo:
                salas_info[sala_id]['tiene_sexo_opuesto'] = True
    
    # DESCARTE: Filtrar camas libres eliminando salas incompatibles
    camas_compatibles = []
    
    for cama in camas_libres:
        sala_id = f"{cama.hospital_id}:{cama.sala}"
        
        # Camas individuales siempre son compatibles
        if not cama.es_sala_compartida:
            camas_compatibles.append(cama)
            continue
        
        # Obtener información de la sala
        info_sala = salas_info.get(sala_id, {'pacientes_actuales': 0, 'tiene_sexo_opuesto': False})
        
        # REGLA 1: Si la sala está vacía, es compatible
        if info_sala['pacientes_actuales'] == 0:
            camas_compatibles.append(cama)
            print(f"✅ Sala {cama.sala}: vacía, compatible para {paciente.sexo.value}")
            continue
        
        # REGLA 2: Si la sala tiene pacientes del sexo opuesto, DESCARTAR
        if info_sala['tiene_sexo_opuesto']:
            print(f"❌ DESCARTANDO sala {cama.sala}: tiene pacientes del sexo opuesto")
            continue
        
        # REGLA 3: Si la sala tiene pacientes del mismo sexo (o sexo no establecido pero hay pacientes), es compatible
        camas_compatibles.append(cama)
        print(f"✅ Sala {cama.sala}: mismo sexo, compatible para {paciente.sexo.value}")
    
    print(f"📊 Descarte completo: {len(camas_libres)} camas → {len(camas_compatibles)} compatibles")
    
    return camas_compatibles


def filtrar_por_servicio(camas: List[Cama], servicio: ServicioEnum) -> List[Cama]:
    """
    Filtra camas por servicio con soporte para MEDICO_QUIRURGICO.
    """
    if servicio in [ServicioEnum.MEDICINA, ServicioEnum.CIRUGIA]:
        # Aceptar camas del servicio específico O del servicio compartido
        return [c for c in camas if c.servicio == servicio or c.servicio == ServicioEnum.MEDICO_QUIRURGICO]
    else:
        # Para otros servicios (UCI, UTI, GINECO, AISLAMIENTO), ser estricto
        return [c for c in camas if c.servicio == servicio]


def filtrar_por_aislamiento(camas: List[Cama], paciente: Paciente) -> List[Cama]:
    """
    Filtra camas según requerimientos de aislamiento.
    """
    
    # Sin aislamiento: cualquier cama
    if paciente.aislamiento == AislamientoEnum.NINGUNO:
        return camas
    
    # Aislamiento que puede compartir (contacto, gotitas)
    if puede_compartir_aislamiento(paciente.aislamiento):
        # Preferir camas que permiten aislamiento compartido
        camas_aislamiento = [c for c in camas if c.permite_aislamiento_compartido]
        if camas_aislamiento:
            print(f"🔍 Filtrado aislamiento compartido: {len(camas)} → {len(camas_aislamiento)} camas con aislamiento")
            return camas_aislamiento
        # Si no hay camas especiales, aceptar cualquier cama compartida
        return camas
    
    # Aislamiento que requiere cama individual (aéreo, ambiente protegido, especial)
    if requiere_aislamiento_individual(paciente.aislamiento):
        # ✅ REGLA CRÍTICA: Verificar si tiene criterios UCI/UTI
        tiene_uci = tiene_requerimientos_uci(paciente.requerimientos)
        tiene_uti = tiene_requerimientos_uti(paciente.requerimientos)
        
        if tiene_uci or tiene_uti:
            # Si tiene criterios UCI/UTI, puede usar cualquier cama individual
            camas_individuales = [c for c in camas if c.es_cama_individual]
            print(f"🔍 Filtrado aislamiento individual (con criterios UCI/UTI): {len(camas)} → {len(camas_individuales)} camas individuales")
            return camas_individuales
        else:
            # Si NO tiene criterios UCI/UTI, SOLO puede usar camas de AISLAMIENTO
            camas_aislamiento_individual = [c for c in camas if c.es_cama_individual and c.servicio == ServicioEnum.AISLAMIENTO]
            print(f"🔍 Filtrado aislamiento individual (SIN criterios UCI/UTI): {len(camas)} → {len(camas_aislamiento_individual)} camas de AISLAMIENTO")
            return camas_aislamiento_individual
    
    return camas


def priorizar_camas(camas: List[Cama], paciente: Paciente) -> List[Cama]:
    """
    Prioriza camas según criterios de asignación óptima.
    """
    
    def calcular_prioridad(cama: Cama) -> Tuple[int, int, int, int]:
        """
        Calcula tupla de prioridad (menor = mejor).
        """
        
        servicio_requerido = determinar_servicio_requerido(paciente)
        
        # Prioridad 1: Coincidencia de servicio
        if cama.servicio == servicio_requerido:
            prioridad_servicio = 0  # Coincidencia exacta
        elif cama.servicio == ServicioEnum.MEDICO_QUIRURGICO and servicio_requerido in [ServicioEnum.MEDICINA, ServicioEnum.CIRUGIA]:
            prioridad_servicio = 1  # Servicio compartido aceptable
        else:
            prioridad_servicio = 2  # Otros servicios
        
        # Prioridad 2: Coincidencia de complejidad
        if cama.complejidad == paciente.complejidad_requerida:
            prioridad_complejidad = 0
        else:
            prioridad_complejidad = abs(
                ["baja", "media", "alta"].index(cama.complejidad.value) -
                ["baja", "media", "alta"].index(paciente.complejidad_requerida.value)
            )
        
        # Prioridad 3: Tipo de cama (individual vs compartida)
        if requiere_aislamiento_individual(paciente.aislamiento):
            # Requiere individual: preferir individuales
            prioridad_tipo = 0 if cama.es_cama_individual else 1
        else:
            # No requiere individual: preferir compartidas
            prioridad_tipo = 0 if cama.es_sala_compartida else 1
        
        # Prioridad 4: Número de sala (ordenar por sala para llenar progresivamente)
        prioridad_sala = cama.numero
        
        return (prioridad_servicio, prioridad_complejidad, prioridad_tipo, prioridad_sala)
    
    camas_ordenadas = sorted(camas, key=calcular_prioridad)
    
    return camas_ordenadas


# ============================================
# FUNCIÓN PRINCIPAL DE ASIGNACIÓN
# ============================================

def asignar_cama(
    paciente: Paciente,
    camas_disponibles: List[Cama],
    todas_camas: List[Cama],
    session,
    solo_verificar: bool = False  # ✅ NUEVO: solo verificar sin asignar
) -> Optional[Cama]:
  
    # Reutilizamos la lógica de buscar candidatos y devolvemos la primera (la mejor)
    candidatos = buscar_candidatos_cama(paciente, camas_disponibles, todas_camas, limite=1)
    if candidatos:
        return candidatos[0]
    return None

def buscar_candidatos_cama(
    paciente: Paciente, 
    camas_disponibles: List[Cama],
    todas_camas: List[Cama],
    limite: int = 3
) -> List[Cama]:
    """
    Busca y devuelve una lista de las mejores camas candidatas para un paciente.
    Útil para mostrar opciones al usuario.
    """
    if not camas_disponibles:
        return []
    
    # 1. Filtrar por compatibilidad de sexo en salas compartidas
    camas_compatibles_sexo = descartar_salas_sexo_incompatible(
        camas_disponibles, 
        todas_camas, 
        paciente
    )
    
    if not camas_compatibles_sexo:
        return []
    
    # 2. Determinar servicio requerido
    servicio_requerido = determinar_servicio_requerido(paciente)
    
    if servicio_requerido is None:
        return []
    
    # 3. Filtrar por servicio requerido
    camas_servicio = filtrar_por_servicio(camas_compatibles_sexo, servicio_requerido)
    
    if not camas_servicio:
        return []
    
    # 4. Filtrar por aislamiento
    camas_aislamiento = filtrar_por_aislamiento(camas_servicio, paciente)
    
    if not camas_aislamiento:
        return []
    
    # 5. Priorizar camas
    camas_priorizadas = priorizar_camas(camas_aislamiento, paciente)
    
    # 6. Filtrar aquellas que realmente se pueden ocupar (verificar sala llena de nuevo por seguridad)
    candidatos_finales = []
    for cama in camas_priorizadas:
        camas_misma_sala = [c for c in todas_camas if c.sala == cama.sala and c.hospital_id == cama.hospital_id]
        if puede_asignar_sala_compartida(cama, paciente, camas_misma_sala):
            candidatos_finales.append(cama)
            if len(candidatos_finales) >= limite:
                break
                
    return candidatos_finales


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
        cama_asignada = asignar_cama(paciente, list(camas_restantes.values()), todas_camas, session)
        
        if cama_asignada:
            # Remover la cama de las disponibles
            del camas_restantes[cama_asignada.id]
            asignaciones.append((paciente, cama_asignada))
        else:
            # Si no se asigna, sigue pendiente
            asignaciones.append((paciente, None))
            
    return asignaciones


# ============================================
# LÓGICA DE TRASLADO
# ============================================

def requiere_cambio_cama(paciente: Paciente, cama_actual: Cama, camas_disponibles=None) -> bool:
    """
    Determina si un paciente requiere cambio de cama.
    ✅ CORRECCIÓN: Detecta cambios en aislamiento y tipo de enfermedad
    ✅ CORRECCIÓN: NO mueve pacientes UCI/UTI que ya están en cama individual con aislamiento
    ✅ CORRECCIÓN: Pacientes en AISLAMIENTO que requieren UCI/UTI deben trasladarse
    """
    
    if not cama_actual:
        return False
    
    # ✅ PRIORIDAD 1: Verificar cambio en aislamiento individual
    if requiere_aislamiento_individual(paciente.aislamiento):
        
        # CASO 1: Ya está en cama individual (UCI, UTI, AISLAMIENTO)
        if cama_actual.es_cama_individual:
            # Verificar si tiene criterios para estar en UCI/UTI
            if cama_actual.servicio in [ServicioEnum.UCI, ServicioEnum.UTI]:
                # Si tiene criterios UCI/UTI, puede quedarse
                if cama_actual.servicio == ServicioEnum.UCI and tiene_requerimientos_uci(paciente.requerimientos):
                    print(f"✅ Paciente en UCI con aislamiento individual y criterios UCI, puede permanecer")
                    return False
                elif cama_actual.servicio == ServicioEnum.UTI and tiene_requerimientos_uti(paciente.requerimientos):
                    print(f"✅ Paciente en UTI con aislamiento individual y criterios UTI, puede permanecer")
                    return False
                else:
                    # Está en UCI/UTI sin criterios → debe ir a AISLAMIENTO
                    print(f"⚠️ REQUIERE CAMBIO: Paciente en {cama_actual.servicio.value} sin criterios {cama_actual.servicio.value} → AISLAMIENTO")
                    return True
            
            # Si está en AISLAMIENTO
            if cama_actual.servicio == ServicioEnum.AISLAMIENTO:
                # ✅ NUEVA CORRECCIÓN: Si está en AISLAMIENTO pero tiene criterios UCI/UTI, DEBE moverse
                tiene_uci = tiene_requerimientos_uci(paciente.requerimientos)
                tiene_uti = tiene_requerimientos_uti(paciente.requerimientos)
                
                if tiene_uci or tiene_uti:
                    print(f"⚠️ REQUIERE CAMBIO: Paciente en AISLAMIENTO desarrolló requerimientos UCI/UTI")
                    return True
                
                # Si no tiene requerimientos UCI/UTI, está bien en aislamiento
                print(f"✅ Paciente ya está en AISLAMIENTO individual")
                return False
        
        # CASO 2: Está en sala compartida → DEBE moverse a AISLAMIENTO individual
        else:
            print(f"⚠️ REQUIERE CAMBIO: Paciente necesita aislamiento individual pero está en sala compartida")
            return True
    
    # ✅ PRIORIDAD 2: Verificar cambio de servicio
    servicio_requerido = determinar_servicio_requerido(paciente, camas_disponibles)
    servicio_actual = cama_actual.servicio
    
    # Si debe darse de alta
    if servicio_requerido is None:
        return False  # Se manejará por requiere_alta
    
    # Si cambió el servicio requerido
    # Considerar MEDICO_QUIRURGICO como compatible con MEDICINA y CIRUGIA
    if servicio_actual == ServicioEnum.MEDICO_QUIRURGICO:
        # Si está en MEDICO_QUIRURGICO, solo cambiar si necesita UCI, UTI, GINECO o AISLAMIENTO
        if servicio_requerido not in [ServicioEnum.MEDICINA, ServicioEnum.CIRUGIA, ServicioEnum.MEDICO_QUIRURGICO]:
            print(f"⚠️ REQUIERE CAMBIO: {servicio_actual.value} → {servicio_requerido.value}")
            return True
    elif servicio_requerido != servicio_actual:
        # Para otros casos, si cambió el servicio, requiere traslado
        print(f"⚠️ REQUIERE CAMBIO: {servicio_actual.value} → {servicio_requerido.value}")
        return True
    
    return False


def requiere_alta(paciente: Paciente) -> bool:
    """
    Determina si un paciente debe ser dado de alta.
    """
    # Si tiene caso especial activo, NO dar de alta
    if paciente.caso_sociosanitario or paciente.espera_cardio:
        print(f"⚠️ NO DAR ALTA: Paciente tiene caso especial (sociosanitario={paciente.caso_sociosanitario}, cardio={paciente.espera_cardio})")
        return False
    
    # Si no tiene requerimientos, sugiere alta
    if len(paciente.requerimientos) == 0:
        print("✅ SUGERIR ALTA: Sin requerimientos y sin casos especiales")
        return True
    
    # Si SOLO tiene requerimientos que NO definen hospitalización, sugiere alta
    requerimientos_validos = [req for req in paciente.requerimientos 
                              if req not in REQUERIMIENTOS_SIN_HOSPITALIZACION]
    
    if len(requerimientos_validos) == 0:
        print(f"✅ SUGERIR ALTA: Solo requerimientos no-hospitalarios ({paciente.requerimientos})")
        return True
    
    print(f"⚠️ NO DAR ALTA: Tiene requerimientos válidos ({requerimientos_validos})")
    return False



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
        
        # Por servicio
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
# FUNCIONES DE GESTIÓN DE SALAS COMPARTIDAS
# ============================================

def actualizar_sexo_sala(cama: Cama, paciente: Paciente, todas_camas: List[Cama], session):
    """
    Actualiza el sexo de la sala cuando se asigna un paciente.
    CRÍTICO: Cuenta OCUPADA y PENDIENTE_TRASLADO
    """
    if not cama.es_sala_compartida:
        return
    
    # Obtener todas las camas de la misma sala
    camas_misma_sala = [c for c in todas_camas if c.sala == cama.sala and c.hospital_id == cama.hospital_id]
    
    # Contar pacientes en la sala (OCUPADA y PENDIENTE_TRASLADO, excluyendo la cama actual)
    pacientes_en_sala = sum(1 for c in camas_misma_sala 
                           if c.id != cama.id 
                           and c.estado in [EstadoCamaEnum.OCUPADA, EstadoCamaEnum.PENDIENTE_TRASLADO])
    
    print(f"📊 Sala {cama.sala}: {pacientes_en_sala} pacientes antes de asignar")
    
    if pacientes_en_sala == 0:
        # Primera ocupación, establecer sexo de la sala
        print(f"✅ Primera ocupación en Sala {cama.sala}, estableciendo sexo: {paciente.sexo.value}")
        for c in camas_misma_sala:
            c.sexo_sala = paciente.sexo
            c.pacientes_en_sala = 1
            session.add(c)
    else:
        # Incrementar contador
        print(f"➕ Incrementando contador en Sala {cama.sala}: {pacientes_en_sala} → {pacientes_en_sala + 1}")
        for c in camas_misma_sala:
            c.pacientes_en_sala = pacientes_en_sala + 1
            session.add(c)


def liberar_sexo_sala(cama: Cama, todas_camas: List[Cama], session):
    """
    Actualiza el sexo de la sala cuando se libera un paciente.
    """
    if not cama.es_sala_compartida:
        return
    
    camas_misma_sala = [c for c in todas_camas if c.sala == cama.sala and c.hospital_id == cama.hospital_id]
    
    for c in camas_misma_sala:
        c.pacientes_en_sala = max(0, c.pacientes_en_sala - 1)
        if c.pacientes_en_sala == 0:
            c.sexo_sala = None  # Sala vacía, liberar restricción de sexo
        session.add(c)