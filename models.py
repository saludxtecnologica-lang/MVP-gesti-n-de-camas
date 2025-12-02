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
    MEDICO_QUIRURGICO = "medico_quirurgico"


class EstadoCamaEnum(str, Enum):
    LIBRE = "libre"
    OCUPADA = "ocupada"
    PENDIENTE_TRASLADO = "pendiente_traslado"  # Amarillo - cama destino
    EN_TRASLADO = "en_traslado"  # Naranja - cama origen
    ALTA_SUGERIDA = "alta_sugerida"  # Azul - sin requerimientos
    REQUIERE_BUSQUEDA_CAMA = "requiere_busqueda_cama"  # Morado - necesita nueva cama


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
    CONTACTO = "contacto"
    GOTITAS = "gotitas"
    AEREO = "aereo"
    AMBIENTE_PROTEGIDO = "ambiente_protegido"
    AISLAMIENTO_ESPECIAL = "aislamiento_especial"


class ComplejidadEnum(str, Enum):
    BAJA = "baja"
    MEDIA = "media"
    ALTA = "alta"


class TipoPacienteEnum(str, Enum):
    """Tipos de pacientes para priorización en cola"""
    HOSPITALIZADO = "hospitalizado"
    URGENCIA = "urgencia"
    DERIVADO = "derivado"
    AMBULATORIO = "ambulatorio"
    PENDIENTE_TRASLADO = "pendiente_traslado"

# ============================================
# REQUERIMIENTOS CLÍNICOS CON PUNTUACIÓN
# ============================================

REQUERIMIENTOS_PUNTOS = {
    # Baja complejidad (1 punto cada uno)
    "tratamiento_endovenoso": 1,
    "dolor_intenso": 1,
    "oxigeno_naricera": 1,
    "oxigeno_mascarilla_multiventuri": 1,
    "aspiracion_invasiva": 1,
    "control_examenes_sangre_2mas": 1,
    "curaciones_alta_complejidad": 1,
    "irrigacion_vesical": 1,
    "observacion_riesgo_compromiso": 1,
    "procedimiento_invasivo_medico": 1,
    
    # Complejidad UTI (3 puntos cada uno)
    "drogas_vasoactivas": 3,
    "monitorizacion_continua": 3,
    "oxigeno_mascarilla_reservorio": 3,
    "oxigeno_cnaf": 3,
    "oxigeno_vmni": 3,
    "dialisis_aguda": 3,
    "bic_insulina": 3,
    
    # Complejidad UCI (5 puntos cada uno)
    "oxigeno_vmi": 5,
    "procuramiento_organos_tejidos": 5,
    
    # Requerimientos que NO definen cama (0 puntos)
    "kinesioterapia_respiratoria": 0,
    "curaciones_heridas": 0,
    "control_examenes_sangre_1vez": 0,
    "tratamiento_endovenoso_2menos": 0,
}

REQUERIMIENTOS_SIN_HOSPITALIZACION = [
    "kinesioterapia_respiratoria",
    "curaciones_heridas",
    "control_examenes_sangre_1vez",
    "tratamiento_endovenoso_2menos"
]

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


# ============================================
# FUNCIONES AUXILIARES - COMPLEJIDAD
# ============================================

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
    """Determina la complejidad según el puntaje total."""
    if puntos >= 5:
        return ComplejidadEnum.ALTA
    elif puntos >= 3:
        return ComplejidadEnum.MEDIA
    else:
        return ComplejidadEnum.BAJA


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
# MODELOS SQLMODEL
# ============================================

class Hospital(SQLModel, table=True):
    """Modelo de Hospital"""
    id: str = Field(primary_key=True)
    nombre: str
    codigo: str = Field(unique=True)
    
    camas: List["Cama"] = Relationship(back_populates="hospital")
    pacientes: List["Paciente"] = Relationship(back_populates="hospital")


class Paciente(SQLModel, table=True):
    """Modelo de Paciente"""
    id: str = Field(primary_key=True)
    hospital_id: str = Field(foreign_key="hospital.id")
    
    # Datos Básicos
    nombre: str
    run: str
    sexo: SexoEnum
    edad: int
    edad_categoria: EdadCategoriaEnum
    enfermedad: EnfermedadEnum
    tipo_paciente: Optional[TipoPacienteEnum] = Field(default=None)
    aislamiento: AislamientoEnum = AislamientoEnum.NINGUNO
    fecha_ingreso: datetime = Field(default_factory=datetime.utcnow)
    
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
    
    # ============================================
    # CAMPOS DE ESTADO DE ASIGNACIÓN Y COLA
    # ============================================
    
    # Estado básico de espera (paciente sin cama asignada)
    en_espera: bool = True
    tiempo_espera_min: int = 0
    
    # Asignación de cama
    cama_id: Optional[str] = Field(default=None, foreign_key="cama.id")
    cama_destino_id: Optional[str] = Field(default=None)
    
    # ============================================
    # CAMPOS PARA COLA DE PRIORIDAD
    # ============================================
    
    # Flag que indica si el paciente está en la cola de prioridad
    en_lista_espera: bool = Field(default=False)
    
    # Flag que indica si el paciente requiere una nueva cama (para traslados)
    requiere_nueva_cama: bool = Field(default=False)
    
    # Prioridad calculada para la cola
    prioridad_calculada: float = Field(default=0.0)
    
    # Timestamp de ingreso a la cola
    timestamp_ingreso_cola: Optional[datetime] = Field(default=None)
    
    # ============================================
    # CAMPOS LEGACY (mantener compatibilidad)
    # ============================================
    
    # Control manual de búsqueda de cama (legacy - usar requiere_nueva_cama)
    requiere_busqueda_cama: bool = Field(default=False)
    motivo_cambio_cama: Optional[str] = Field(default=None)
    
    # Control de traslado automático
    requiere_aprobacion_traslado: bool = False
    motivo_traslado_pendiente: Optional[str] = Field(default=None)
    
    # Derivación entre hospitales
    hospital_origen_id: Optional[str] = Field(default=None)
    hospital_derivacion_id: Optional[str] = Field(default=None)
    derivacion_pendiente: bool = False
    derivacion_aceptada: bool = False  # Agregado para compatibilidad
    motivo_derivacion: Optional[str] = Field(default=None)
    motivo_rechazo_derivacion: Optional[str] = Field(default=None)
    cama_origen_id: Optional[str] = Field(default=None)
    egreso_confirmado: bool = False
    paciente_aceptado_destino: bool = False  # Paciente ya aceptado en hospital destino, pero cama origen aún no egresada
    timestamp_aceptacion_derivacion: Optional[datetime] = Field(default=None)  # Marca cuándo fue aceptado
    
    # Flag adicional para cambio de cama (diferente de requiere_nueva_cama)
    requiere_cambio_cama: bool = Field(default=False)

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
    """Modelo de Cama"""
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


# ============================================
# CONFIGURACIÓN DE CAMAS POR HOSPITAL
# ============================================

def get_configuracion_inicial_camas_escalado(hospital_id: str, tipo_hospital: str = "completo") -> List[Dict[str, Any]]:
    """
    Retorna la configuración inicial de camas para un hospital según su tipo.
    """
    camas = []
    
    if tipo_hospital == "basico":
        cama_letras = ['A', 'B', 'C', 'D']
        for sala_num in range(1, 5):
            for letra in cama_letras:
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
    
    # UCI - 3 camas individuales
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

    # UTI - 3 camas individuales
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

    # MEDICINA - 3 salas compartidas de 3 camas
    cama_letras = ['A', 'B', 'C']
    for sala_num in range(1, 4):
        num_sala = 500 + sala_num
        for letra in cama_letras:
            camas.append({
                "id": f"{hospital_id}-{num_sala}-{letra}",
                "hospital_id": hospital_id,
                "servicio": ServicioEnum.MEDICINA,
                "sala": f"Sala {num_sala}",
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
            
    # AISLAMIENTO - 3 camas individuales
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
            
    # CIRUGÍA - 3 salas compartidas de 3 camas
    for sala_num in range(1, 4):
        num_sala = 600 + sala_num
        for letra in cama_letras:
            camas.append({
                "id": f"{hospital_id}-{num_sala}-{letra}",
                "hospital_id": hospital_id,
                "servicio": ServicioEnum.CIRUGIA,
                "sala": f"Sala {num_sala}",
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
            
    # GINECOLOGÍA - 2 salas compartidas de 3 camas
    for sala_num in range(1, 3):
        num_sala = 603 + sala_num
        for letra in cama_letras:
            camas.append({
                "id": f"{hospital_id}-{num_sala}-{letra}",
                "hospital_id": hospital_id,
                "servicio": ServicioEnum.GINECO,
                "sala": f"Sala {num_sala}",
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
        hospital = Hospital(id=config["id"], nombre=config["nombre"], codigo=config["id"])
        session.add(hospital)
        
        camas_data = get_configuracion_inicial_camas_escalado(config["id"], config["tipo"])
        for c_data in camas_data:
            cama = Cama(**c_data)
            session.add(cama)
        
        total_camas += len(camas_data)
        print(f"✅ {config['nombre']}: {len(camas_data)} camas")
    
    session.commit()
    print(f"\n✅ Sistema multi-hospitalario inicializado con {total_camas} camas totales.")


def get_configuracion_inicial_camas(hospital_id: str) -> List[Dict[str, Any]]:
    """Retorna la configuración inicial de camas para un hospital."""
    return get_configuracion_inicial_camas_escalado(hospital_id, "completo")


def init_db_with_data(session, hospital_id: str = "HOSP-001", hospital_nombre: str = "Hospital Central"):
    """Inicializa la base de datos con un hospital y sus camas."""
    hospital = Hospital(id=hospital_id, nombre=hospital_nombre, codigo=hospital_id)
    session.add(hospital)
    
    camas_data = get_configuracion_inicial_camas(hospital_id)
    for c_data in camas_data:
        cama = Cama(**c_data)
        session.add(cama)
        
    session.commit()
    print(f"✅ Base de datos inicializada con {len(camas_data)} camas para {hospital_nombre}.")