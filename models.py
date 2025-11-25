from typing import Optional, List, Dict, Any
from enum import Enum
from sqlmodel import SQLModel, Field, Relationship, Column, JSON
from datetime import datetime


# ============================================
# ENUMS
# ============================================

class ServicioEnum(str, Enum):
    UCI = "uci"
    UTI = "uti"
    MEDICINA = "medicina"
    CIRUGIA = "cirugia"
    GINECO = "gineco"
    AISLAMIENTO = "aislamiento"
    MEDICO_QUIRURGICO = "medico_quirurgico"  # Nuevo servicio compartido


class EstadoCamaEnum(str, Enum):
    LIBRE = "libre"
    OCUPADA = "ocupada"
    PENDIENTE_TRASLADO = "pendiente_traslado"  # Amarillo - cama destino
    EN_TRASLADO = "en_traslado"  # Naranja - cama origen (esperando confirmación)
    ALTA_SUGERIDA = "alta_sugerida"  # Azul - paciente sin requerimientos
    REQUIERE_BUSQUEDA_CAMA = "requiere_busqueda_cama"  # 🆕 NUEVO - Morado - indica que el paciente necesita nueva cama pero aún no se ha buscado


class SexoEnum(str, Enum):
    HOMBRE = "hombre"
    MUJER = "mujer"


class EdadCategoriaEnum(str, Enum):
    ADULTO = "adulto"
    ADULTO_MAYOR = "adulto_mayor"
    NINO = "niño"
    ADOLESCENTE = "adolescente"
    LACTANTE = "lactante"


class EnfermedadEnum(str, Enum):
    MEDICA = "medica"
    QUIRURGICA = "quirurgica"
    GINECOLOGICA = "ginecologica"
    OBSTETRICA = "obstetrica"
    TRAUMATOLOGICA = "traumatologica"
    NEUROLOGICA = "neurologica"
    GERIATRICA = "geriatrica"
    UROLOGICA = "urologica"


class AislamientoEnum(str, Enum):
    NINGUNO = "ninguno"
    CONTACTO = "contacto"  # Puede compartir sala
    GOTITAS = "gotitas"  # Puede compartir sala
    AEREO = "aereo"  # Cama individual exclusiva
    AMBIENTE_PROTEGIDO = "ambiente_protegido"  # Cama individual exclusiva
    AISLAMIENTO_ESPECIAL = "aislamiento_especial"  # C. difficile, KPC, otros - Cama individual exclusiva


class ComplejidadEnum(str, Enum):
    BAJA = "baja"  # 1-2 puntos - Sala básica
    MEDIA = "media"  # 3-4 puntos - UTI
    ALTA = "alta"  # 5+ puntos - UCI


# ============================================
# REQUERIMIENTOS CLÍNICOS CON PUNTUACIÓN
# ============================================

REQUERIMIENTOS_PUNTOS = {
    # Baja complejidad (1 punto cada uno)
    "tratamiento_endovenoso": 1,
    "dolor_intenso": 1,  # EVA >= 7
    "oxigeno_naricera": 1,
    "oxigeno_mascarilla_multiventuri": 1,
    "aspiracion_invasiva": 1,
    "control_examenes_sangre_2mas": 1,  # Control con examen de sangre 2+ veces
    "curaciones_alta_complejidad": 1,  # VAC, gran quemados, etc.
    "irrigacion_vesical": 1,
    "observacion_riesgo_compromiso": 1,  # Observación por riesgo de compromiso clínico
    "procedimiento_invasivo_medico": 1,  # Procedimiento invasivo realizado por médico
    
    # Complejidad UTI (3 puntos cada uno)
    "drogas_vasoactivas": 3,
    "monitorizacion_continua": 3,
    "oxigeno_mascarilla_reservorio": 3,
    "oxigeno_cnaf": 3,
    "oxigeno_vmni": 3,  # Ventilación mecánica no invasiva
    "dialisis_aguda": 3,
    "bic_insulina": 3,  # BIC insulina
    
    # Complejidad UCI (5 puntos cada uno)
    "oxigeno_vmi": 5,  # Ventilación mecánica invasiva
    "procuramiento_organos_tejidos": 5,
    
    # Requerimientos que NO definen cama ni necesidad de hospitalización (0 puntos)
    "kinesioterapia_respiratoria": 0,
    "curaciones_heridas": 0,
    "control_examenes_sangre_1vez": 0,
    "tratamiento_endovenoso_2menos": 0,  # Tratamiento endovenoso por 2 veces o menos
}

# Requerimientos que NO definen cama ni necesidad de hospitalización
REQUERIMIENTOS_SIN_HOSPITALIZACION = [
    "kinesioterapia_respiratoria",
    "curaciones_heridas",
    "control_examenes_sangre_1vez",
    "tratamiento_endovenoso_2menos"
]

# Requerimientos específicos que determinan UCI/UTI
REQUERIMIENTOS_UCI = [
    "oxigeno_vmi",
    "procuramiento_organos_tejidos"
]

REQUERIMIENTOS_UTI = [
    "drogas_vasoactivas",
    "monitorizacion_continua",
    "oxigeno_mascarilla_reservorio",
    "oxigeno_cnaf",
    "oxigeno_vmni",
    "dialisis_aguda",
    "bic_insulina"
]


def calcular_puntos_complejidad(requerimientos: List[str]) -> int:
    """Calcula el puntaje total de complejidad basado en requerimientos."""
    return sum(REQUERIMIENTOS_PUNTOS.get(req.lower(), 0) for req in requerimientos)


def tiene_requerimientos_uci(requerimientos: List[str]) -> bool:
    """Verifica si el paciente tiene requerimientos específicos de UCI."""
    return any(req.lower() in [r.lower() for r in REQUERIMIENTOS_UCI] for req in requerimientos)


def tiene_requerimientos_uti(requerimientos: List[str]) -> bool:
    """Verifica si el paciente tiene requerimientos específicos de UTI."""
    return any(req.lower() in [r.lower() for r in REQUERIMIENTOS_UTI] for req in requerimientos)


def determinar_complejidad_por_puntos(puntos: int) -> ComplejidadEnum:
    """Determina la complejidad según el puntaje total (solo para priorización)."""
    if puntos >= 5:
        return ComplejidadEnum.ALTA  # UCI
    elif puntos >= 3:
        return ComplejidadEnum.MEDIA  # UTI
    else:
        return ComplejidadEnum.BAJA  # Sala básica


def determinar_categoria_edad(edad: int) -> EdadCategoriaEnum:
    """Determina la categoría de edad según los años."""
    if edad < 2:
        return EdadCategoriaEnum.LACTANTE
    elif 2 <= edad < 12:
        return EdadCategoriaEnum.NINO
    elif 12 <= edad < 18:
        return EdadCategoriaEnum.ADOLESCENTE
    elif 18 <= edad < 65:
        return EdadCategoriaEnum.ADULTO
    else:
        return EdadCategoriaEnum.ADULTO_MAYOR


# ============================================
# MODELOS
# ============================================

class Hospital(SQLModel, table=True):
    id: str = Field(primary_key=True)
    nombre: str
    codigo: str = Field(unique=True)
    
    # Relaciones
    camas: List["Cama"] = Relationship(back_populates="hospital")
    pacientes: List["Paciente"] = Relationship(back_populates="hospital")


class Paciente(SQLModel, table=True):
    id: str = Field(primary_key=True)
    hospital_id: str = Field(foreign_key="hospital.id")
    
    # Datos Básicos
    nombre: str
    run: str  # RUN del paciente
    sexo: SexoEnum
    edad: int  # Edad en años
    edad_categoria: EdadCategoriaEnum
    enfermedad: EnfermedadEnum
    aislamiento: AislamientoEnum = AislamientoEnum.NINGUNO
    ingreso: datetime = Field(default_factory=datetime.utcnow)  # ✅ CORREGIDO: nombre correcto
    
    # Características Especiales
    es_embarazada: bool = False
    es_adulto_mayor: bool = False
    caso_sociosanitario: bool = False
    espera_cardio: bool = False
    
    # Requerimientos y Estado Clínico
    requerimientos: List[str] = Field(sa_column=Column(JSON), default=[])
    complejidad_requerida: ComplejidadEnum = ComplejidadEnum.BAJA
    puntos_complejidad: int = 0
    
    # Diagnóstico y detalles clínicos
    diagnostico: Optional[str] = Field(default=None)
    motivo_monitorizacion: Optional[str] = Field(default=None)
    signos_monitorizacion: Optional[str] = Field(default=None)
    notas: Optional[str] = Field(default=None)
    detalle_procedimiento_invasivo: Optional[str] = Field(default=None)
    
    # Estado de asignación
    en_espera: bool = True
    tiempo_espera_min: int = 0
    cama_id: Optional[str] = Field(default=None, foreign_key="cama.id")
    cama_destino_id: Optional[str] = Field(default=None)
    
    # 🆕 NUEVO: Control manual de búsqueda de cama
    requiere_cambio_cama: bool = Field(default=False)  # ✅ Indica que necesita cambiar de cama
    motivo_cambio_cama: Optional[str] = Field(default=None)  # Razón por la que necesita cambiar de cama
    
    # Control de traslado automático (legacy - se mantiene por compatibilidad)
    requiere_aprobacion_traslado: bool = False
    motivo_traslado_pendiente: Optional[str] = Field(default=None)
    
    # Derivación entre hospitales
    hospital_origen_id: Optional[str] = Field(default=None)
    hospital_derivacion_id: Optional[str] = Field(default=None)
    derivacion_pendiente: bool = False
    motivo_derivacion: Optional[str] = Field(default=None)
    motivo_rechazo_derivacion: Optional[str] = Field(default=None)
    cama_origen_id: Optional[str] = Field(default=None)
    egreso_confirmado: bool = False

    # Relaciones
    hospital: Hospital = Relationship(back_populates="pacientes")
    cama: Optional["Cama"] = Relationship(
        back_populates="paciente",
        sa_relationship_kwargs={
            "foreign_keys": "[Paciente.cama_id]",
            "post_update": True
        }
    )


class Cama(SQLModel, table=True):
    id: str = Field(primary_key=True)
    hospital_id: str = Field(foreign_key="hospital.id")
    
    # Ubicación y Tipo
    servicio: ServicioEnum
    sala: str
    numero: int
    
    # Características
    complejidad: ComplejidadEnum = ComplejidadEnum.BAJA
    permite_aislamiento: bool = False
    permite_aislamiento_compartido: bool = False
    es_cama_individual: bool = False
    
    # Salas compartidas
    es_sala_compartida: bool = False
    capacidad_sala: int = 1
    sexo_sala: Optional[SexoEnum] = None
    pacientes_en_sala: int = 0
    
    # Estado actual
    estado: EstadoCamaEnum = EstadoCamaEnum.LIBRE
    paciente_id: Optional[str] = Field(default=None)

    # Relaciones
    hospital: Hospital = Relationship(back_populates="camas")
    paciente: Optional["Paciente"] = Relationship(
        back_populates="cama",
        sa_relationship_kwargs={
            "foreign_keys": "[Paciente.cama_id]"
        }
    )


def get_configuracion_inicial_camas_escalado(hospital_id: str, tipo_hospital: str = "completo") -> List[Dict[str, Any]]:
    """
    Retorna la configuración inicial de camas para un hospital según su tipo.
    
    Args:
        hospital_id: ID del hospital
        tipo_hospital: "completo" para Puerto Montt, "basico" para Calbuco/Llanquihue
    """
    camas = []
    
    if tipo_hospital == "basico":
        # HOSPITALES SECUNDARIOS (Calbuco, Llanquihue): 4 salas de 4 camas = 16 camas
        
        # 4 SALAS COMPARTIDAS DE 4 CAMAS = 16 CAMAS MÉDICO-QUIRÚRGICAS
        cama_letras = ['A', 'B', 'C', 'D']
        for sala_num in range(1, 5):  # Salas 1-4
            for letra in cama_letras:  # 4 camas por sala (A, B, C, D)
                camas.append({
                    "id": f"{hospital_id}-{sala_num}-{letra}",
                    "hospital_id": hospital_id,
                    "servicio": ServicioEnum.MEDICO_QUIRURGICO,
                    "sala": f"Sala {sala_num}",
                    "numero": sala_num,
                    "estado": EstadoCamaEnum.LIBRE,
                    "complejidad": ComplejidadEnum.BAJA,
                    "permite_aislamiento_compartido": False,
                    "es_cama_individual": False,
                    "es_sala_compartida": True,
                    "capacidad_sala": 4,
                    "sexo_sala": None,
                    "pacientes_en_sala": 0
                })
        
        return camas
    
    # HOSPITAL COMPLETO (Puerto Montt)
    
    # UCI - 3 camas individuales (200-202)
    for i in range(1, 4):
        camas.append({
            "id": f"{hospital_id}-{200 + i - 1}",
            "hospital_id": hospital_id,
            "servicio": ServicioEnum.UCI,
            "sala": f"UCI",
            "numero": 200 + i - 1,
            "estado": EstadoCamaEnum.LIBRE,
            "complejidad": ComplejidadEnum.ALTA,
            "permite_aislamiento": True,
            "es_cama_individual": True,
            "es_sala_compartida": False,
            "capacidad_sala": 1,
            "sexo_sala": None,
            "pacientes_en_sala": 0
        })

    # UTI - 3 camas individuales (203-205)
    for i in range(1, 4):
        camas.append({
            "id": f"{hospital_id}-{202 + i}",
            "hospital_id": hospital_id,
            "servicio": ServicioEnum.UTI,
            "sala": f"UTI",
            "numero": 202 + i,
            "estado": EstadoCamaEnum.LIBRE,
            "complejidad": ComplejidadEnum.MEDIA,
            "permite_aislamiento": True,
            "es_cama_individual": True,
            "es_sala_compartida": False,
            "capacidad_sala": 1,
            "sexo_sala": None,
            "pacientes_en_sala": 0
        })

    # MEDICINA - 3 salas compartidas de 3 camas = 9 camas (501-A, 501-B, 501-C, 502-A, etc.)
    cama_letras = ['A', 'B', 'C']
    for sala_num in range(1, 4):  # 3 salas (501, 502, 503)
        num_sala = 500 + sala_num
        for cama_idx, letra in enumerate(cama_letras):
            camas.append({
                "id": f"{hospital_id}-{num_sala}-{letra}",
                "hospital_id": hospital_id,
                "servicio": ServicioEnum.MEDICINA,
                "sala": f"Sala {num_sala}",  # Cada sala con su número
                "numero": num_sala,
                "estado": EstadoCamaEnum.LIBRE,
                "complejidad": ComplejidadEnum.BAJA,
                "permite_aislamiento_compartido": False,
                "es_cama_individual": False,
                "es_sala_compartida": True,
                "capacidad_sala": 3,
                "sexo_sala": None,
                "pacientes_en_sala": 0
            })
            
    # AISLAMIENTO - 3 camas individuales (504, 505, 506) - después de medicina
    for i in range(1, 4):
        camas.append({
            "id": f"{hospital_id}-{503 + i}",
            "hospital_id": hospital_id,
            "servicio": ServicioEnum.AISLAMIENTO,
            "sala": "Aislamiento",
            "numero": 503 + i,
            "estado": EstadoCamaEnum.LIBRE,
            "complejidad": ComplejidadEnum.BAJA,
            "permite_aislamiento": True,
            "es_cama_individual": True,
            "es_sala_compartida": False,
            "capacidad_sala": 1,
            "sexo_sala": None,
            "pacientes_en_sala": 0
        })
            
    # CIRUGÍA - 3 salas compartidas de 3 camas = 9 camas (601-A, 601-B, 601-C, 602-A, etc.)
    for sala_num in range(1, 4):  # 3 salas (601, 602, 603)
        num_sala = 600 + sala_num
        for cama_idx, letra in enumerate(cama_letras):
            camas.append({
                "id": f"{hospital_id}-{num_sala}-{letra}",
                "hospital_id": hospital_id,
                "servicio": ServicioEnum.CIRUGIA,
                "sala": f"Sala {num_sala}",  # Cada sala con su número
                "numero": num_sala,
                "estado": EstadoCamaEnum.LIBRE,
                "complejidad": ComplejidadEnum.BAJA,
                "permite_aislamiento_compartido": False,
                "es_cama_individual": False,
                "es_sala_compartida": True,
                "capacidad_sala": 3,
                "sexo_sala": None,
                "pacientes_en_sala": 0
            })
            
    # GINECOLOGÍA - 2 salas compartidas de 3 camas = 6 camas (604-A, 604-B, 604-C, 605-A, etc.)
    for sala_num in range(1, 3):  # 2 salas (604, 605)
        num_sala = 603 + sala_num
        for cama_idx, letra in enumerate(cama_letras):
            camas.append({
                "id": f"{hospital_id}-{num_sala}-{letra}",
                "hospital_id": hospital_id,
                "servicio": ServicioEnum.GINECO,
                "sala": f"Sala {num_sala}",  # Cada sala con su número
                "numero": num_sala,
                "estado": EstadoCamaEnum.LIBRE,
                "complejidad": ComplejidadEnum.BAJA,
                "permite_aislamiento_compartido": False,
                "es_cama_individual": False,
                "es_sala_compartida": True,
                "capacidad_sala": 3,
                "sexo_sala": None,
                "pacientes_en_sala": 0
            })
    
    return camas


def init_multihospital_system(session):
    """
    Inicializa el sistema con 3 hospitales: Puerto Montt (completo), Calbuco y Llanquihue (básicos).
    """
    hospitales_config = [
        {"id": "PMONTT", "nombre": "Hospital Puerto Montt", "tipo": "completo"},
        {"id": "CALBUCO", "nombre": "Hospital Calbuco", "tipo": "basico"},
        {"id": "LLANHUE", "nombre": "Hospital Llanquihue", "tipo": "basico"},
    ]
    
    total_camas = 0
    for config in hospitales_config:
        # Crear hospital
        hospital = Hospital(id=config["id"], nombre=config["nombre"], codigo=config["id"])
        session.add(hospital)
        
        # Crear camas según tipo de hospital
        camas_data = get_configuracion_inicial_camas_escalado(config["id"], config["tipo"])
        for c_data in camas_data:
            cama = Cama(**c_data)
            session.add(cama)
        
        total_camas += len(camas_data)
        print(f"✅ {config['nombre']}: {len(camas_data)} camas")
    
    session.commit()
    print(f"\n✅ Sistema multi-hospitalario inicializado con {total_camas} camas totales en {len(hospitales_config)} hospitales.")


def get_configuracion_inicial_camas(hospital_id: str) -> List[Dict[str, Any]]:
    """
    Retorna la configuración inicial de camas para un hospital (versión completa).
    """
    return get_configuracion_inicial_camas_escalado(hospital_id, "completo")


def init_db_with_data(session, hospital_id: str = "HOSP-001", hospital_nombre: str = "Hospital Central"):
    """
    Inicializa la base de datos con un hospital y sus camas.
    """
    # Crear hospital
    hospital = Hospital(id=hospital_id, nombre=hospital_nombre, codigo=hospital_id)
    session.add(hospital)
    
    # Crear camas
    camas_data = get_configuracion_inicial_camas(hospital_id)
    for c_data in camas_data:
        cama = Cama(**c_data)
        session.add(cama)
        
    session.commit()
    print(f"✅ Base de datos inicializada con {len(camas_data)} camas para {hospital_nombre}.")