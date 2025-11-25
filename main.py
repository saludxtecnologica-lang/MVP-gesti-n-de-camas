from fastapi import FastAPI, HTTPException, Depends, WebSocket, WebSocketDisconnect, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from fastapi.encoders import jsonable_encoder
from sqlmodel import SQLModel, create_engine, Session, select
from contextlib import asynccontextmanager
from typing import List, Optional, Dict, Set
from datetime import datetime
import os
import json
import uuid
import random

# Intentar importar Redis, pero hacerlo opcional
try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    print("⚠️ Redis no está instalado. Funcionará sin Redis (solo notificaciones WebSocket)")

from models import (
    Hospital, Cama, Paciente,
    ServicioEnum, EstadoCamaEnum, SexoEnum, EdadCategoriaEnum,
    EnfermedadEnum, AislamientoEnum,
    get_configuracion_inicial_camas,
    determinar_categoria_edad
)

from logic import (
    asignar_camas_batch,
    asignar_cama,
    actualizar_complejidad_paciente,
    determinar_servicio_requerido,
    requiere_cambio_cama,
    requiere_alta,
    actualizar_sexo_sala,
    liberar_sexo_sala,
    buscar_candidatos_cama
)


# ============================================
# CONFIGURACIÓN
# ============================================

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./hospital.db")
connect_args = {"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
engine = create_engine(DATABASE_URL, echo=False, connect_args=connect_args)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
redis_client: Optional[redis.Redis] = None if not REDIS_AVAILABLE else None


# ============================================
# LIFESPAN EVENTS
# ============================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client
    
    print("🏥 Inicializando base de datos...")
    SQLModel.metadata.create_all(engine)
    print("✅ Base de datos inicializada")
    
    if REDIS_AVAILABLE:
        try:
            redis_client = await redis.from_url(
                REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=2
            )
            await redis_client.ping()
            print("✅ Conectado a Redis exitosamente")
        except Exception as e:
            print(f"⚠️ Redis no disponible: {e}")
            print("ℹ️ La aplicación funcionará sin Redis (solo WebSocket)")
            redis_client = None
    else:
        print("ℹ️ Redis no instalado - usando solo WebSocket")
    
    print("🚀 Servidor listo en http://localhost:8000")
    print("📊 Dashboard disponible en http://localhost:8000/dashboard")
    print("📚 API Docs disponible en http://localhost:8000/docs")
    
    yield
    
    if redis_client:
        await redis_client.close()
        print("❌ Conexión a Redis cerrada")


# ============================================
# CREAR APP
# ============================================

app = FastAPI(
    title="Sistema de Gestión de Camas Hospitalarias",
    description="API para gestión de camas y asignación de pacientes",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================
# DEPENDENCIAS
# ============================================

def get_session():
    with Session(engine) as session:
        yield session


# ============================================
# REDIS Y WEBSOCKET
# ============================================

async def enviar_evento(tipo: str, datos: dict):
    """Publica un mensaje JSON a Redis. Si Redis no está disponible, solo registra en consola."""
    if not redis_client:
        print(f"📢 Evento (sin Redis): {tipo}")
        return
    
    try:
        mensaje = {
            "tipo": tipo,
            **datos,
            "timestamp": datetime.utcnow().isoformat()
        }
        await redis_client.publish("eventos_camas", json.dumps(mensaje))
        print(f"📢 Evento publicado en Redis: {tipo}")
    except Exception as e:
        print(f"❌ Error publicando evento: {e}")


class ConnectionManager:
    """Gestiona las conexiones WebSocket por hospital"""
    
    def __init__(self):
        self.active_connections: Dict[str, Set[WebSocket]] = {}
    
    async def connect(self, websocket: WebSocket, hospital_id: str):
        await websocket.accept()
        if hospital_id not in self.active_connections:
            self.active_connections[hospital_id] = set()
        self.active_connections[hospital_id].add(websocket)
        print(f"✅ Cliente conectado al hospital {hospital_id}. Total: {len(self.active_connections[hospital_id])}")
    
    def disconnect(self, websocket: WebSocket, hospital_id: str):
        if hospital_id in self.active_connections:
            self.active_connections[hospital_id].discard(websocket)
            if not self.active_connections[hospital_id]:
                del self.active_connections[hospital_id]
            print(f"❌ Cliente desconectado del hospital {hospital_id}")
    
    async def broadcast(self, hospital_id: str, message: dict):
        if hospital_id not in self.active_connections:
            return
        
        json_message = json.dumps(message)
        disconnected = set()
        for connection in self.active_connections[hospital_id]:
            try:
                await connection.send_text(json_message)
            except Exception as e:
                print(f"⚠️ Error enviando mensaje: {e}")
                disconnected.add(connection)
        
        for connection in disconnected:
            self.disconnect(connection, hospital_id)


manager = ConnectionManager()


# ============================================
# SCHEMAS PYDANTIC
# ============================================

from pydantic import BaseModel


class PacienteIngreso(BaseModel):
    nombre: str
    run: str  # RUN del paciente en formato 12345678-9
    sexo: SexoEnum
    edad: int  # Edad en años
    edad_categoria: EdadCategoriaEnum
    enfermedad: EnfermedadEnum
    requerimientos: List[str] = []
    aislamiento: AislamientoEnum = AislamientoEnum.NINGUNO
    es_embarazada: bool = False
    es_adulto_mayor: bool = False
    caso_sociosanitario: bool = False
    espera_cardio: bool = False
    diagnostico: Optional[str] = None
    motivo_monitorizacion: Optional[str] = None
    signos_monitorizacion: Optional[str] = None
    notas: Optional[str] = None
    detalle_procedimiento_invasivo: Optional[str] = None


class CamaResponse(BaseModel):
    id: str
    hospital_id: str
    servicio: ServicioEnum
    sala: str
    numero: int
    estado: EstadoCamaEnum
    paciente_id: Optional[str] = None


class PacienteResponse(BaseModel):
    id: str
    hospital_id: str
    nombre: str
    sexo: SexoEnum
    edad_categoria: EdadCategoriaEnum
    enfermedad: EnfermedadEnum
    requerimientos: List[str]
    aislamiento: AislamientoEnum
    en_espera: bool
    tiempo_espera_min: int
    es_embarazada: bool
    es_adulto_mayor: bool
    cama_id: Optional[str] = None


class AsignacionResponse(BaseModel):
    paciente: PacienteResponse
    cama: CamaResponse
    mensaje: str


class ActualizarRequerimientosRequest(BaseModel):
    requerimientos: List[str]
    aislamiento: Optional[AislamientoEnum] = None
    diagnostico: Optional[str] = None
    motivo_monitorizacion: Optional[str] = None
    signos_monitorizacion: Optional[str] = None
    notas: Optional[str] = None
    detalle_procedimiento_invasivo: Optional[str] = None
    caso_sociosanitario: Optional[bool] = None
    espera_cardio: Optional[bool] = None


class ReevaluarPacienteRequest(BaseModel):
    # Campos clínicos
    enfermedad: Optional[EnfermedadEnum] = None
    requerimientos: List[str] = []
    aislamiento: Optional[AislamientoEnum] = None
    
    # Casos especiales
    caso_sociosanitario: Optional[bool] = None
    espera_cardio: Optional[bool] = None
    
    # Detalles adicionales
    diagnostico: Optional[str] = None
    motivo_monitorizacion: Optional[str] = None
    signos_monitorizacion: Optional[str] = None
    notas: Optional[str] = None
    detalle_procedimiento_invasivo: Optional[str] = None


class DerivarPacienteRequest(BaseModel):
    hospital_destino_id: str
    motivo_derivacion: str


class RechazarDerivacionRequest(BaseModel):
    motivo_rechazo: str


class AceptarRechazarDerivacionResponse(BaseModel):
    mensaje: str
    paciente_id: str
    accion: str  # "aceptado" o "rechazado"
    hospital_actual_id: str


# ============================================
# FUNCIÓN AUXILIAR DE NOTIFICACIÓN
# ============================================

async def notificar_cambio(
    hospital_id: str,
    evento: str,
    session: Session,
    detalles: Optional[dict] = None
):
    """Notifica cambios tanto via WebSocket como Redis (si está disponible)."""
    query_camas = select(Cama).where(Cama.hospital_id == hospital_id)
    camas = session.exec(query_camas).all()
    
    # ✅ CORREGIDO: Incluir tanto pacientes en espera normal como derivaciones pendientes
    query_pacientes = select(Paciente).where(
        Paciente.hospital_id == hospital_id
    ).where(
        (Paciente.en_espera == True) | (Paciente.derivacion_pendiente == True)
    ).where(
        Paciente.cama_id == None
    )
    pacientes_espera = session.exec(query_pacientes).all()
    
    mensaje = {
        "evento": evento,
        "hospital_id": hospital_id,
        "timestamp": datetime.utcnow().isoformat(),
        "camas": [jsonable_encoder(cama) for cama in camas],
        "pacientes_espera": [jsonable_encoder(p) for p in pacientes_espera],
        "detalles": detalles or {}
    }
    
    await manager.broadcast(hospital_id, mensaje)
    await enviar_evento(evento, {"hospital_id": hospital_id, **(detalles or {})})


# ============================================
# ENDPOINTS - RAÍZ Y DASHBOARD
# ============================================

@app.get("/", response_class=HTMLResponse)
async def root():
    """Redirige al dashboard"""
    return """
    <html>
        <head>
            <meta http-equiv="refresh" content="0; url=/dashboard" />
        </head>
        <body>
            <p>Redirigiendo al dashboard...</p>
        </body>
    </html>
    """


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """Sirve el dashboard HTML"""
    try:
        with open("dashboard.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(
            content="<h1>Dashboard no encontrado</h1><p>Asegúrate de que dashboard.html esté en el mismo directorio.</p>",
            status_code=404
        )


# ============================================
# ENDPOINTS API SIMPLIFICADOS (PARA DASHBOARD)
# ============================================

@app.get("/api/estadisticas")
def api_estadisticas(
    hospital_id: str = "HOSP-001",
    session: Session = Depends(get_session)
):
    """Endpoint simplificado para estadísticas (usado por el dashboard)."""
    hospital = session.get(Hospital, hospital_id)
    if not hospital:
        return {
            "mensaje": "Hospital no inicializado",
            "total_camas": 0,
            "por_estado": {},
            "por_servicio": {},
            "tasa_ocupacion": 0,
            "pacientes_en_espera": 0
        }
    
    query_camas = select(Cama).where(Cama.hospital_id == hospital_id)
    todas_camas = session.exec(query_camas).all()
    
    if not todas_camas:
        return {
            "mensaje": "Hospital sin camas",
            "total_camas": 0,
            "por_estado": {},
            "por_servicio": {},
            "tasa_ocupacion": 0,
            "pacientes_en_espera": 0
        }
    
    estadisticas = {
        "total_camas": len(todas_camas),
        "por_estado": {},
        "por_servicio": {}
    }
    
    for estado in EstadoCamaEnum:
        count = len([c for c in todas_camas if c.estado == estado])
        estadisticas["por_estado"][estado.value] = count
    
    for servicio in ServicioEnum:
        camas_servicio = [c for c in todas_camas if c.servicio == servicio]
        estadisticas["por_servicio"][servicio.value] = {
            "total": len(camas_servicio),
            "libres": len([c for c in camas_servicio if c.estado == EstadoCamaEnum.LIBRE]),
            "ocupadas": len([c for c in camas_servicio if c.estado == EstadoCamaEnum.OCUPADA])
        }
    
    camas_ocupadas = len([c for c in todas_camas if c.estado == EstadoCamaEnum.OCUPADA])
    estadisticas["tasa_ocupacion"] = round((camas_ocupadas / len(todas_camas)) * 100, 2)
    
    query_espera = select(Paciente).where(
        Paciente.hospital_id == hospital_id,
        Paciente.en_espera == True,
        Paciente.cama_id == None  # Solo pacientes sin cama confirmada
    )
    pacientes_espera = session.exec(query_espera).all()
    estadisticas["pacientes_en_espera"] = len(pacientes_espera)
    
    return estadisticas

# ============================================
# ENDPOINTS - HOSPITALES
# ============================================

@app.get("/hospitales")
def listar_hospitales(session: Session = Depends(get_session)):
    """Lista todos los hospitales"""
    query = select(Hospital)
    hospitales = session.exec(query).all()
    return hospitales


@app.get("/hospitales/{hospital_id}")
def obtener_hospital(hospital_id: str, session: Session = Depends(get_session)):
    """Obtiene información de un hospital específico"""
    hospital = session.get(Hospital, hospital_id)
    if not hospital:
        raise HTTPException(status_code=404, detail=f"Hospital {hospital_id} no encontrado")
    return hospital


@app.post("/hospitales/inicializar")
def inicializar_hospital(
    hospital_id: str = "HOSP-001",
    hospital_nombre: str = "Hospital Central",
    session: Session = Depends(get_session)
):
    """
    Inicializa un hospital con su configuración de camas.
    Útil para desarrollo y testing.
    """
    hospital_existente = session.get(Hospital, hospital_id)
    if hospital_existente:
        raise HTTPException(
            status_code=400,
            detail=f"Hospital {hospital_id} ya existe. Usa DELETE primero si quieres reinicializar."
        )
    
    hospital = Hospital(id=hospital_id, nombre=hospital_nombre)
    session.add(hospital)
    
    camas_config = get_configuracion_inicial_camas(hospital_id)
    for cama_data in camas_config:
        cama = Cama(**cama_data)
        session.add(cama)
    
    session.commit()
    
    print(f"✅ Hospital {hospital_nombre} inicializado con {len(camas_config)} camas")
    
    return {
        "mensaje": f"Hospital {hospital_nombre} inicializado exitosamente",
        "hospital_id": hospital_id,
        "total_camas": len(camas_config)
    }


@app.post("/hospitales/inicializar-multi")
def inicializar_sistema_multihospitalario(session: Session = Depends(get_session)):
    """
    Inicializa el sistema completo con 3 hospitales:
    - Hospital Puerto Montt (PMONTT): 30 camas (3 UCI, 3 UTI, 9 Medicina, 3 Aislamiento, 9 Cirugía, 6 Ginecología)
    - Hospital Calbuco (CALBUCO): 16 camas (4 salas de 4 camas cada una)
    - Hospital Llanquihue (LLANHUE): 16 camas (4 salas de 4 camas cada una)
    """
    # Verificar si ya existen hospitales
    query_hospitales = select(Hospital)
    hospitales_existentes = session.exec(query_hospitales).all()
    
    if hospitales_existentes:
        raise HTTPException(
            status_code=400,
            detail="El sistema ya tiene hospitales inicializados. Usa DELETE para limpiar primero."
        )
    
    # Importar la función de models
    from models import init_multihospital_system
    
    # Inicializar los 3 hospitales
    init_multihospital_system(session)
    
    # Obtener estadísticas
    query_hospitales = select(Hospital)
    hospitales = session.exec(query_hospitales).all()
    
    info_hospitales = []
    total_camas = 0
    for hospital in hospitales:
        query_camas = select(Cama).where(Cama.hospital_id == hospital.id)
        num_camas = len(session.exec(query_camas).all())
        total_camas += num_camas
        info_hospitales.append({
            "id": hospital.id,
            "nombre": hospital.nombre,
            "camas": num_camas
        })
    
    return {
        "mensaje": "Sistema multi-hospitalario inicializado exitosamente",
        "total_hospitales": len(hospitales),
        "total_camas": total_camas,
        "hospitales": info_hospitales
    }


@app.delete("/hospitales/{hospital_id}")
def eliminar_hospital(hospital_id: str, session: Session = Depends(get_session)):
    """Elimina un hospital y todas sus camas y pacientes"""
    hospital = session.get(Hospital, hospital_id)
    if not hospital:
        raise HTTPException(status_code=404, detail=f"Hospital {hospital_id} no encontrado")
    
    # Eliminar camas
    query_camas = select(Cama).where(Cama.hospital_id == hospital_id)
    camas = session.exec(query_camas).all()
    for cama in camas:
        session.delete(cama)
    
    # Eliminar pacientes
    query_pacientes = select(Paciente).where(Paciente.hospital_id == hospital_id)
    pacientes = session.exec(query_pacientes).all()
    for paciente in pacientes:
        session.delete(paciente)
    
    # Eliminar hospital
    session.delete(hospital)
    session.commit()
    
    print(f"✅ Hospital {hospital_id} eliminado")
    
    return {
        "mensaje": f"Hospital {hospital_id} eliminado exitosamente",
        "camas_eliminadas": len(camas),
        "pacientes_eliminados": len(pacientes)
    }


# ============================================
# ENDPOINTS - CAMAS
# ============================================

@app.get("/hospitales/{hospital_id}/camas")
def listar_camas(hospital_id: str, session: Session = Depends(get_session)):
    hospital = session.get(Hospital, hospital_id)
    if not hospital:
        raise HTTPException(status_code=404, detail=f"Hospital {hospital_id} no encontrado")
    
    query = select(Cama).where(Cama.hospital_id == hospital_id)
    camas = session.exec(query).all()
    
    # ✅ NUEVO: Agregar información del paciente a cada cama
    query_todas = select(Cama).where(Cama.hospital_id == hospital_id)
    todas_camas = session.exec(query_todas).all()
    
    # ✅ CORRECCIÓN: Crear una lista de respuesta con información extendida
    camas_con_info = []
    for cama in camas:
        # Convertir la cama a dict
        cama_dict = cama.model_dump()
        
        if cama.paciente_id:
            paciente = session.get(Paciente, cama.paciente_id)
            if paciente:
                # Verificar si requiere cambio
                requiere_cambio = requiere_cambio_cama(paciente, cama, todas_camas)
                
                # Agregar información del paciente al dict
                cama_dict["paciente_info"] = {
                    "id": paciente.id,
                    "nombre": paciente.nombre,
                    "caso_sociosanitario": paciente.caso_sociosanitario,
                    "espera_cardio": paciente.espera_cardio,
                    "requiere_cambio_cama": requiere_cambio  # ✅ NUEVO FLAG
                }
                cama_dict["paciente_nombre"] = paciente.nombre
        
        camas_con_info.append(cama_dict)
    
    return camas_con_info


@app.get("/hospitales/{hospital_id}/camas/{cama_id}")
def obtener_cama(
    hospital_id: str,
    cama_id: str,
    session: Session = Depends(get_session)
):
    """Obtiene información detallada de una cama"""
    cama = session.get(Cama, cama_id)
    
    if not cama:
        raise HTTPException(status_code=404, detail="Cama no encontrada")
    
    if cama.hospital_id != hospital_id:
        raise HTTPException(status_code=400, detail="Cama no pertenece al hospital")
    
    return cama


# ============================================
# ENDPOINTS - PACIENTES
# ============================================

@app.get("/hospitales/{hospital_id}/pacientes")
def listar_pacientes(hospital_id: str, session: Session = Depends(get_session)):
    pacientes = session.exec(select(Paciente).where(Paciente.hospital_id == hospital_id)).all()
    return [
        {
            "id": p.id,
            "nombre": p.nombre,
            "run": p.run,
            "edad": p.edad,
            "edad_categoria": p.edad_categoria.value,
            "enfermedad": p.enfermedad.value,
            "tiempo_espera_min": p.tiempo_espera_min,
            "en_espera": p.en_espera,
            "cama_id": p.cama_id,
            "requiere_cambio_cama": p.requiere_cambio_cama,
            "derivacion_pendiente": p.derivacion_pendiente,  
            "hospital_origen_id": p.hospital_origen_id,  
            "motivo_derivacion": p.motivo_derivacion,  
            "es_embarazada": p.es_embarazada,
            "es_adulto_mayor": p.es_adulto_mayor
        }
        for p in pacientes
    ]


@app.post("/hospitales/{hospital_id}/pacientes/ingresar")
async def ingresar_paciente(
    hospital_id: str,
    paciente_data: PacienteIngreso,
    session: Session = Depends(get_session)
):
    """Ingresa un nuevo paciente al hospital"""
    hospital = session.get(Hospital, hospital_id)
    if not hospital:
        raise HTTPException(status_code=404, detail=f"Hospital {hospital_id} no encontrado")
    
    # Calcular automáticamente edad_categoria basándose en la edad numérica
    edad_categoria_calculada = determinar_categoria_edad(paciente_data.edad)
    
    # Determinar si es adulto mayor basándose en edad_categoria calculada
    es_adulto_mayor_calculado = (edad_categoria_calculada == EdadCategoriaEnum.ADULTO_MAYOR)
    
    # Crear el paciente con edad_categoria y es_adulto_mayor calculados
    data_dict = paciente_data.model_dump()
    data_dict['edad_categoria'] = edad_categoria_calculada
    data_dict['es_adulto_mayor'] = es_adulto_mayor_calculado
    
    paciente = Paciente(
        id=f"PAC-{uuid.uuid4().hex[:8].upper()}",
        hospital_id=hospital_id,
        **data_dict,
        en_espera=True,
        tiempo_espera_min=0
    )
    
    # ✅ Actualizar complejidad
    actualizar_complejidad_paciente(paciente)
    
    session.add(paciente)
    session.commit()
    session.refresh(paciente)
    
    print(f"✅ Paciente ingresado: {paciente.nombre} (ID: {paciente.id})")
    
    await notificar_cambio(
        hospital_id=hospital_id,
        evento="paciente_ingresado",
        session=session,
        detalles={
            "paciente_id": paciente.id,
            "paciente_nombre": paciente.nombre
        }
    )
    
    return {
        "mensaje": "Paciente ingresado exitosamente (sin camas disponibles en este momento)",
        "paciente": paciente
    }


@app.get("/hospitales/{hospital_id}/pacientes/{paciente_id}")
def obtener_paciente_detalle(
    hospital_id: str,
    paciente_id: str,
    session: Session = Depends(get_session)
):
    """Obtiene información detallada completa de un paciente"""
    paciente = session.get(Paciente, paciente_id)
    
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    
    if paciente.hospital_id != hospital_id:
        raise HTTPException(status_code=400, detail="Paciente no pertenece al hospital")
    
    # Obtener información de la cama si tiene
    cama_info = None
    if paciente.cama_id:
        cama = session.get(Cama, paciente.cama_id)
        if cama:
            cama_info = {
                "id": cama.id,
                "servicio": cama.servicio.value,
                "sala": cama.sala,
                "numero": cama.numero,
                "estado": cama.estado.value
            }
    
    return {
        "id": paciente.id,
        "hospital_id": paciente.hospital_id,
        "nombre": paciente.nombre,
        "run": paciente.run,
        "sexo": paciente.sexo.value,
        "edad": paciente.edad,
        "edad_categoria": paciente.edad_categoria.value,
        "enfermedad": paciente.enfermedad.value,
        "aislamiento": paciente.aislamiento.value,
        "requerimientos": paciente.requerimientos,
        "complejidad_requerida": paciente.complejidad_requerida.value,
        "puntos_complejidad": paciente.puntos_complejidad,
        "es_embarazada": paciente.es_embarazada,
        "es_adulto_mayor": paciente.es_adulto_mayor,
        "caso_sociosanitario": paciente.caso_sociosanitario,
        "espera_cardio": paciente.espera_cardio,
        "diagnostico": paciente.diagnostico,
        "motivo_monitorizacion": paciente.motivo_monitorizacion,
        "signos_monitorizacion": paciente.signos_monitorizacion,
        "notas": paciente.notas,
        "detalle_procedimiento_invasivo": paciente.detalle_procedimiento_invasivo,
        "en_espera": paciente.en_espera,
        "tiempo_espera_min": paciente.tiempo_espera_min,
        "cama_id": paciente.cama_id,
        "cama_info": cama_info,
        "requiere_cambio_cama": paciente.requiere_cambio_cama,  # ✅ INCLUIR FLAG
        "motivo_cambio_cama": paciente.motivo_cambio_cama,
        "ingreso": paciente.ingreso.isoformat() if paciente.ingreso else None,  # ✅ CORREGIDO
        "derivacion_pendiente": paciente.derivacion_pendiente,
        "hospital_origen_id": paciente.hospital_origen_id,
        "motivo_derivacion": paciente.motivo_derivacion,
        "motivo_rechazo_derivacion": paciente.motivo_rechazo_derivacion
    }


@app.get("/hospitales/{hospital_id}/pacientes/{paciente_id}/opciones-cambio-cama")
async def obtener_opciones_cambio_cama(
    hospital_id: str,
    paciente_id: str,
    session: Session = Depends(get_session)
):
    """✅ Obtiene las opciones disponibles cuando un paciente requiere cambio de cama"""
    
    paciente = session.get(Paciente, paciente_id)
    
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    
    if paciente.hospital_id != hospital_id:
        raise HTTPException(status_code=400, detail="Paciente no pertenece al hospital")
    
    # Buscar camas disponibles en hospital local
    query_camas_locales = select(Cama).where(
        Cama.hospital_id == hospital_id,
        Cama.estado == EstadoCamaEnum.LIBRE
    )
    camas_locales = session.exec(query_camas_locales).all()
    
    query_todas_locales = select(Cama).where(Cama.hospital_id == hospital_id)
    todas_camas_locales = session.exec(query_todas_locales).all()
    
    # Buscar hasta 3 candidatos
    candidatos = buscar_candidatos_cama(
        paciente=paciente,
        camas_disponibles=camas_locales,
        todas_camas=todas_camas_locales,
        limite=3
    )
    
    opciones = []
    for idx, cama in enumerate(candidatos):
        opciones.append({
            "cama_id": cama.id,
            "servicio": cama.servicio.value,
            "sala": cama.sala,
            "numero": cama.numero,
            "prioridad": idx + 1,
            "razon": f"Servicio: {cama.servicio.value}, Complejidad: {cama.complejidad.value}"
        })
    
    return {
        "opciones": opciones,
        "total_opciones": len(opciones)
    }


@app.post("/hospitales/{hospital_id}/pacientes/{paciente_id}/confirmar-opcion-cama")
async def confirmar_opcion_cambio_cama(
    hospital_id: str,
    paciente_id: str,
    datos: dict,
    session: Session = Depends(get_session)
):
    """✅ Confirma la selección de una cama específica para el cambio"""
    
    cama_id = datos.get("cama_id")
    if not cama_id:
        raise HTTPException(status_code=400, detail="Debe especificar cama_id")
    
    paciente = session.get(Paciente, paciente_id)
    
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    
    if paciente.hospital_id != hospital_id:
        raise HTTPException(status_code=400, detail="Paciente no pertenece al hospital")
    
    nueva_cama = session.get(Cama, cama_id)
    
    if not nueva_cama:
        raise HTTPException(status_code=404, detail="Cama no encontrada")
    
    if nueva_cama.hospital_id != hospital_id:
        raise HTTPException(status_code=400, detail="Cama no pertenece al hospital")
    
    if nueva_cama.estado != EstadoCamaEnum.LIBRE:
        raise HTTPException(status_code=400, detail="La cama no está disponible")
    
    # Obtener cama actual
    cama_actual = None
    if paciente.cama_id:
        cama_actual = session.get(Cama, paciente.cama_id)
        if cama_actual:
            # ✅ CRÍTICO: Guardar cama_origen_id ANTES de marcar como EN_TRASLADO
            # Esto permite que completar-traslado encuentre y libere la cama origen
            if not paciente.cama_origen_id:  # Solo si no está ya establecido (evitar sobrescribir derivaciones)
                paciente.cama_origen_id = cama_actual.id
                print(f"💾 GUARDANDO cama_origen_id: {paciente.cama_origen_id}")
            
            cama_actual.estado = EstadoCamaEnum.EN_TRASLADO
            session.add(cama_actual)
            
            print(f"🔄 Traslado interno iniciado:")
            print(f"   - Cama origen: {cama_actual.id} (EN_TRASLADO)")
            print(f"   - Paciente: {paciente.nombre}")
            print(f"   - cama_origen_id guardado: {paciente.cama_origen_id}")
    
    # Marcar nueva cama como pendiente
    nueva_cama.estado = EstadoCamaEnum.PENDIENTE_TRASLADO
    nueva_cama.paciente_id = paciente.id
    paciente.cama_destino_id = nueva_cama.id
    paciente.requiere_cambio_cama = False  # ✅ Limpiar flag
    paciente.motivo_cambio_cama = None
    
    session.add(nueva_cama)
    session.add(paciente)
    session.commit()
    session.refresh(nueva_cama)
    
    await notificar_cambio(
        hospital_id=hospital_id,
        evento="cambio_cama_iniciado",
        session=session,
        detalles={
            "paciente_id": paciente.id,
            "cama_origen_id": cama_actual.id if cama_actual else None,
            "cama_destino_id": nueva_cama.id
        }
    )
    
    return {
        "mensaje": "Cambio de cama iniciado exitosamente",
        "cama_destino": jsonable_encoder(nueva_cama)
    }


# ============================================
# ENDPOINTS - ASIGNACIÓN DE CAMAS
# ============================================

@app.post("/hospitales/{hospital_id}/asignar-camas")
async def asignar_camas_a_pacientes(
    hospital_id: str,
    session: Session = Depends(get_session)
):
    """Asigna camas a todos los pacientes en espera según prioridad"""
    hospital = session.get(Hospital, hospital_id)
    if not hospital:
        raise HTTPException(status_code=404, detail=f"Hospital {hospital_id} no encontrado")
    
    # ✅ CORREGIDO: Solo pacientes realmente en espera (sin cama confirmada)
    query_pacientes = select(Paciente).where(
        Paciente.hospital_id == hospital_id,
        Paciente.en_espera == True,
        Paciente.cama_id == None  # No tienen cama confirmada todavía
    )
    pacientes_espera = session.exec(query_pacientes).all()
    
    if not pacientes_espera:
        return {
            "mensaje": "No hay pacientes en espera",
            "asignaciones": [],
            "pacientes_sin_asignar": []
        }
    
    query_camas = select(Cama).where(
        Cama.hospital_id == hospital_id,
        Cama.estado == EstadoCamaEnum.LIBRE
    )
    camas_disponibles = session.exec(query_camas).all()
    
    if not camas_disponibles:
        return {
            "mensaje": "No hay camas disponibles",
            "asignaciones": [],
            "pacientes_sin_asignar": [p.nombre for p in pacientes_espera]
        }
    
    # Obtener TODAS las camas para el descarte de salas incompatibles
    query_todas = select(Cama).where(Cama.hospital_id == hospital_id)
    todas_camas_hospital = session.exec(query_todas).all()
    
    asignaciones = asignar_camas_batch(pacientes_espera, camas_disponibles, todas_camas_hospital, session)
    
    resultado_asignaciones = []
    pacientes_sin_asignar = []
    
    for paciente, cama in asignaciones:
        if cama:
            # ✅ CORREGIDO: Solo RESERVAR la cama, no ocuparla
            # La cama pasa a PENDIENTE_TRASLADO (amarillo) esperando confirmación
            cama.estado = EstadoCamaEnum.PENDIENTE_TRASLADO
            cama.paciente_id = paciente.id  # Reservar para este paciente
            paciente.cama_destino_id = cama.id  # Guardar cama asignada
            paciente.en_espera = True  # Sigue en espera hasta confirmar traslado
            
            session.add(cama)
            session.add(paciente)
            
            resultado_asignaciones.append({
                "paciente": jsonable_encoder(paciente),
                "cama": jsonable_encoder(cama)
            })
            
            print(f"✅ Cama reservada: {paciente.nombre} → Cama {cama.id} (PENDIENTE CONFIRMACIÓN)")
        else:
            pacientes_sin_asignar.append(paciente.nombre)
            print(f"⚠️ Sin cama disponible: {paciente.nombre}")
    
    session.commit()
    
    await notificar_cambio(
        hospital_id=hospital_id,
        evento="asignaciones_realizadas",
        session=session,
        detalles={
            "total_asignaciones": len(resultado_asignaciones),
            "pacientes_sin_asignar": len(pacientes_sin_asignar)
        }
    )
    
    return {
        "mensaje": f"Se asignaron {len(resultado_asignaciones)} camas exitosamente",
        "asignaciones": resultado_asignaciones,
        "pacientes_sin_asignar": pacientes_sin_asignar
    }


# ============================================
# ENDPOINTS - REEVALUACIÓN
# ============================================

@app.put("/hospitales/{hospital_id}/pacientes/{paciente_id}/reevaluar")
async def reevaluar_paciente_completo(
    hospital_id: str,
    paciente_id: str,
    request_data: ReevaluarPacienteRequest,
    session: Session = Depends(get_session)
):
    """
    Reevaluación completa del paciente: actualiza enfermedad, requerimientos, aislamiento y casos especiales.
    Si hay cambios que requieren traslado de cama, marca el flag requiere_cambio_cama.
    """
    
    paciente = session.get(Paciente, paciente_id)
    
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    
    if paciente.hospital_id != hospital_id:
        raise HTTPException(status_code=400, detail="Paciente no pertenece al hospital")
    
    # Guardar valores anteriores para detectar cambios
    enfermedad_anterior = paciente.enfermedad
    aislamiento_anterior = paciente.aislamiento
    
    # Actualizar enfermedad si se proporciona
    if request_data.enfermedad is not None:
        paciente.enfermedad = request_data.enfermedad
    
    # Actualizar requerimientos
    paciente.requerimientos = request_data.requerimientos
    
    # Actualizar aislamiento si se proporciona
    if request_data.aislamiento is not None:
        paciente.aislamiento = request_data.aislamiento
    
    # Actualizar casos especiales si se proporcionan
    if request_data.caso_sociosanitario is not None:
        paciente.caso_sociosanitario = request_data.caso_sociosanitario
    if request_data.espera_cardio is not None:
        paciente.espera_cardio = request_data.espera_cardio
    
    # Actualizar campos adicionales
    if request_data.diagnostico is not None:
        paciente.diagnostico = request_data.diagnostico
    if request_data.motivo_monitorizacion is not None:
        paciente.motivo_monitorizacion = request_data.motivo_monitorizacion
    if request_data.signos_monitorizacion is not None:
        paciente.signos_monitorizacion = request_data.signos_monitorizacion
    if request_data.notas is not None:
        paciente.notas = request_data.notas
    if request_data.detalle_procedimiento_invasivo is not None:
        paciente.detalle_procedimiento_invasivo = request_data.detalle_procedimiento_invasivo
    
    # Actualizar complejidad
    actualizar_complejidad_paciente(paciente)
    
    session.add(paciente)
    
    # 1. Verificar si requiere alta (solo si NO tiene casos especiales activos)
    if requiere_alta(paciente):
        cama_actual = session.get(Cama, paciente.cama_id) if paciente.cama_id else None
        if cama_actual and cama_actual.estado == EstadoCamaEnum.OCUPADA:
            cama_actual.estado = EstadoCamaEnum.ALTA_SUGERIDA
            session.add(cama_actual)
            session.commit()
            
            await notificar_cambio(
                hospital_id=hospital_id,
                evento="alta_automatica_sugerida",
                session=session,
                detalles={
                    "paciente_id": paciente.id,
                    "paciente_nombre": paciente.nombre,
                    "cama_id": cama_actual.id,
                    "motivo": "Paciente sin requerimientos clínicos y sin casos especiales"
                }
            )
            
            return {
                "mensaje": "Alta sugerida automáticamente",
                "paciente_id": paciente.id,
                "requiere_alta": True,
                "requiere_cambio_cama": False,
                "cambio_enfermedad": enfermedad_anterior != paciente.enfermedad,
                "cambio_aislamiento": aislamiento_anterior != paciente.aislamiento,
                "cama_actual_id": cama_actual.id
            }
    
    # 2. Verificar si requiere cambio de cama
    cama_actual = session.get(Cama, paciente.cama_id) if paciente.cama_id else None
    
    if cama_actual:
        # Obtener todas las camas para la verificación
        query_todas = select(Cama).where(Cama.hospital_id == hospital_id)
        todas_camas = session.exec(query_todas).all()
        
        if requiere_cambio_cama(paciente, cama_actual, todas_camas):
            # Determinar motivo del cambio
            motivo_cambio = []
            if enfermedad_anterior != paciente.enfermedad:
                motivo_cambio.append(f"Cambio de enfermedad: {enfermedad_anterior.value} → {paciente.enfermedad.value}")
            if aislamiento_anterior != paciente.aislamiento:
                motivo_cambio.append(f"Cambio de aislamiento: {aislamiento_anterior.value} → {paciente.aislamiento.value}")
            if not motivo_cambio:
                servicio_requerido = determinar_servicio_requerido(paciente, todas_camas)
                motivo_cambio.append(f"Requiere servicio: {servicio_requerido.value if servicio_requerido else 'N/A'}")
            
            motivo_str = " | ".join(motivo_cambio)
            
            # ✅ Marcar el paciente como que requiere cambio de cama
            paciente.requiere_cambio_cama = True
            paciente.motivo_cambio_cama = motivo_str
            
            session.add(paciente)
            session.commit()
            
            await notificar_cambio(
                hospital_id=hospital_id,
                evento="requiere_cambio_cama",
                session=session,
                detalles={
                    "paciente_id": paciente.id,
                    "paciente_nombre": paciente.nombre,
                    "cama_actual_id": cama_actual.id,
                    "motivo": motivo_str,
                    "servicio_requerido": determinar_servicio_requerido(paciente, todas_camas).value if determinar_servicio_requerido(paciente, todas_camas) else None
                }
            )
            
            return {
                "mensaje": "Paciente reevaluado. Requiere cambio de cama - Use botón 'Asignar Nueva Cama'",
                "paciente_id": paciente.id,
                "requiere_cambio_cama": True,
                "requiere_alta": False,
                "cambio_enfermedad": enfermedad_anterior != paciente.enfermedad,
                "cambio_aislamiento": aislamiento_anterior != paciente.aislamiento,
                "cama_actual_id": cama_actual.id,
                "motivo_cambio": motivo_str,
                "servicio_requerido": determinar_servicio_requerido(paciente, todas_camas).value if determinar_servicio_requerido(paciente, todas_camas) else None,
                "complejidad": paciente.complejidad_requerida.value,
                "puntos": paciente.puntos_complejidad
            }
    
    # 3. No requiere cambios de cama
    session.commit()
    
    await notificar_cambio(
        hospital_id=hospital_id,
        evento="paciente_reevaluado",
        session=session,
        detalles={
            "paciente_id": paciente.id,
            "paciente_nombre": paciente.nombre,
            "cambio_enfermedad": enfermedad_anterior != paciente.enfermedad,
            "cambio_aislamiento": aislamiento_anterior != paciente.aislamiento,
            "complejidad": paciente.complejidad_requerida.value,
            "puntos": paciente.puntos_complejidad
        }
    )
    
    return {
        "mensaje": "Paciente reevaluado exitosamente",
        "paciente_id": paciente.id,
        "requiere_cambio_cama": False,
        "requiere_alta": False,
        "cambio_enfermedad": enfermedad_anterior != paciente.enfermedad,
        "cambio_aislamiento": aislamiento_anterior != paciente.aislamiento,
        "servicio_actual": cama_actual.servicio.value if cama_actual else None,
        "complejidad": paciente.complejidad_requerida.value,
        "puntos": paciente.puntos_complejidad
    }


# ============================================
# ENDPOINTS - TRASLADOS
# ============================================

@app.post("/hospitales/{hospital_id}/pacientes/{paciente_id}/confirmar-traslado")
async def confirmar_traslado_paciente(
    hospital_id: str,
    paciente_id: str,
    session: Session = Depends(get_session)
):
    """
    Confirma que el paciente ha sido trasladado a la cama destino.
    - Libera la cama origen
    - Marca la cama destino como OCUPADA (verde)
    - Actualiza la referencia de cama del paciente
    """
    paciente = session.get(Paciente, paciente_id)
    
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    
    if paciente.hospital_id != hospital_id:
        raise HTTPException(status_code=400, detail="Paciente no pertenece al hospital")
    
    if not paciente.cama_destino_id:
        raise HTTPException(status_code=400, detail="Paciente no tiene traslado pendiente")
    
    cama_origen = session.get(Cama, paciente.cama_id) if paciente.cama_id else None
    cama_destino = session.get(Cama, paciente.cama_destino_id)
    
    if not cama_destino:
        raise HTTPException(status_code=404, detail="Cama destino no encontrada")
    
    # 1. Liberar cama origen
    if cama_origen:
        # Obtener todas las camas para actualizar sala compartida
        query_todas = select(Cama).where(Cama.hospital_id == hospital_id)
        todas_camas = session.exec(query_todas).all()
        
        # Liberar sala compartida si aplica
        liberar_sexo_sala(cama_origen, todas_camas, session)
        
        cama_origen.estado = EstadoCamaEnum.LIBRE
        cama_origen.paciente_id = None
        session.add(cama_origen)
    
    # 2. Ocupar cama destino y actualizar sexo de sala
    query_todas_destino = select(Cama).where(Cama.hospital_id == hospital_id)
    todas_camas_destino = session.exec(query_todas_destino).all()
    actualizar_sexo_sala(cama_destino, paciente, todas_camas_destino, session)
    
    cama_destino.estado = EstadoCamaEnum.OCUPADA
    cama_destino.paciente_id = paciente.id
    session.add(cama_destino)
    
    # 3. Actualizar paciente
    paciente.cama_id = cama_destino.id
    paciente.cama_destino_id = None
    paciente.en_espera = False  # ✅ Ya no está en espera, está acostado
    session.add(paciente)
    
    session.commit()
    session.refresh(cama_destino)
    
    await notificar_cambio(
        hospital_id=hospital_id,
        evento="traslado_confirmado",
        session=session,
        detalles={
            "paciente_id": paciente.id,
            "paciente_nombre": paciente.nombre,
            "cama_origen_id": cama_origen.id if cama_origen else None,
            "cama_destino_id": cama_destino.id
        }
    )
    
    return {
        "mensaje": "Traslado confirmado exitosamente",
        "paciente_id": paciente.id,
        "cama_origen_id": cama_origen.id if cama_origen else None,
        "cama_destino_id": cama_destino.id
    }


@app.post("/hospitales/{hospital_id}/pacientes/{paciente_id}/cancelar-traslado")
async def cancelar_traslado_paciente(
    hospital_id: str,
    paciente_id: str,
    session: Session = Depends(get_session)
):
    """
    Cancela un traslado pendiente. El paciente vuelve a su cama original 
    (si venía de una) o a lista de espera (si es nuevo).
    """
    paciente = session.get(Paciente, paciente_id)
    
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    
    if paciente.hospital_id != hospital_id:
        raise HTTPException(status_code=400, detail="Paciente no pertenece al hospital")
    
    if not paciente.cama_destino_id:
        raise HTTPException(status_code=400, detail="Paciente no tiene traslado pendiente")
    
    cama_destino = session.get(Cama, paciente.cama_destino_id)
    cama_origen = session.get(Cama, paciente.cama_id) if paciente.cama_id else None
    
    # 1. Liberar cama destino (la que estaba pendiente)
    if cama_destino:
        cama_destino.estado = EstadoCamaEnum.LIBRE
        cama_destino.paciente_id = None
        session.add(cama_destino)
    
    # 2. Decidir dónde va el paciente
    if cama_origen:
        # Si venía de una cama, volver a ocuparla
        cama_origen.estado = EstadoCamaEnum.OCUPADA
        session.add(cama_origen)
        paciente.cama_destino_id = None
        mensaje = f"Traslado cancelado. Paciente {paciente.nombre} permanece en {cama_origen.id}"
    else:
        # Si era nuevo (no tenía cama origen), volver a lista de espera
        paciente.en_espera = True
        paciente.cama_id = None
        paciente.cama_destino_id = None
        mensaje = f"Traslado cancelado. Paciente {paciente.nombre} vuelve a lista de espera"
    
    session.add(paciente)
    session.commit()
    
    await notificar_cambio(
        hospital_id=hospital_id,
        evento="traslado_cancelado",
        session=session,
        detalles={
            "paciente_id": paciente.id,
            "paciente_nombre": paciente.nombre,
            "cama_destino_liberada": cama_destino.id if cama_destino else None,
            "volvio_a_cama_origen": cama_origen.id if cama_origen else None,
            "volvio_a_espera": not cama_origen
        }
    )
    
    print(f"✅ {mensaje}")
    
    return {
        "mensaje": mensaje,
        "paciente_id": paciente.id,
        "en_espera": paciente.en_espera,
        "cama_actual": paciente.cama_id
    }


@app.post("/hospitales/{hospital_id}/camas/{cama_id}/completar-traslado")
async def completar_traslado(
    hospital_id: str,
    cama_id: str,
    session: Session = Depends(get_session)
):
    """
    ✅ CORREGIDO: Completa el traslado confirmando que el paciente llegó a la cama destino.
    
    Flujo:
    1. Cama destino está en PENDIENTE_TRASLADO
    2. Se confirma que el paciente llegó
    3. Paciente se asigna a esta cama (OCUPADA)
    4. Cama origen se libera
    """
    cama_destino = session.get(Cama, cama_id)
    
    if not cama_destino:
        raise HTTPException(status_code=404, detail="Cama no encontrada")
    
    if cama_destino.hospital_id != hospital_id:
        raise HTTPException(status_code=400, detail="Cama no pertenece al hospital")
    
    # ✅ CORRECCIÓN: La cama debe estar en PENDIENTE_TRASLADO (no EN_TRASLADO)
    if cama_destino.estado != EstadoCamaEnum.PENDIENTE_TRASLADO:
        raise HTTPException(
            status_code=400, 
            detail=f"Solo se puede completar traslado en cama PENDIENTE_TRASLADO. Estado actual: {cama_destino.estado}"
        )
    
    paciente_id = cama_destino.paciente_id
    if not paciente_id:
        raise HTTPException(status_code=400, detail="No hay paciente asignado a esta cama")
    
    paciente = session.get(Paciente, paciente_id)
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    
    # ✅ CRÍTICO: Obtener cama origen usando cama_origen_id (NO cama_id)
    # En derivaciones, cama_id se limpia pero cama_origen_id se mantiene
    cama_origen = None
    if paciente.cama_origen_id:
        cama_origen = session.get(Cama, paciente.cama_origen_id)
        print(f"🔍 Buscando cama origen: {paciente.cama_origen_id}")
    else:
        print(f"⚠️ paciente.cama_origen_id es None")
    
    # ✅ PASO 1: Liberar cama origen
    if cama_origen:
        print(f"📍 Cama origen encontrada: {cama_origen.id}, estado: {cama_origen.estado}")
        if cama_origen.estado == EstadoCamaEnum.EN_TRASLADO:
            # Liberar cama origen y actualizar sexo de sala si es compartida
            cama_origen.estado = EstadoCamaEnum.LIBRE
            cama_origen.paciente_id = None
            
            # Obtener todas las camas para actualizar sexo de sala
            query_todas = select(Cama).where(Cama.hospital_id == cama_origen.hospital_id)
            todas_camas = session.exec(query_todas).all()
            
            # Liberar sexo de sala si es compartida
            liberar_sexo_sala(cama_origen, todas_camas, session)
            
            session.add(cama_origen)
            print(f"✅ Cama origen {cama_origen.id} liberada correctamente")
        else:
            print(f"⚠️ Cama origen {cama_origen.id} no está EN_TRASLADO (estado: {cama_origen.estado})")
    else:
        print(f"⚠️ No se encontró cama origen (cama_origen_id: {paciente.cama_origen_id})")
    
    # ✅ PASO 2: Asignar paciente a cama destino
    cama_destino.estado = EstadoCamaEnum.OCUPADA
    cama_destino.paciente_id = paciente.id
    paciente.cama_id = cama_destino.id
    paciente.cama_origen_id = None  # ✅ Limpiar después de completar
    paciente.en_espera = False
    
    # Actualizar sexo de sala si es compartida
    query_todas_destino = select(Cama).where(Cama.hospital_id == hospital_id)
    todas_camas_destino = session.exec(query_todas_destino).all()
    actualizar_sexo_sala(cama_destino, paciente, todas_camas_destino, session)
    
    session.add(cama_destino)
    session.add(paciente)
    session.commit()
    session.refresh(cama_destino)
    session.refresh(paciente)
    
    await notificar_cambio(
        hospital_id=hospital_id,
        evento="traslado_completado",
        session=session,
        detalles={
            "cama_origen_id": cama_origen.id if cama_origen else None,
            "cama_destino_id": cama_destino.id,
            "paciente_id": paciente.id,
            "paciente_nombre": paciente.nombre
        }
    )
    
    print(f"✅ Traslado completado: Paciente {paciente.nombre} movido a cama {cama_destino.id}")
    
    return {
        "mensaje": f"Traslado completado. Paciente asignado a {cama_destino.id}",
        "cama_destino_id": cama_destino.id,
        "cama_origen_id": cama_origen.id if cama_origen else None,
        "paciente": {
            "id": paciente.id,
            "nombre": paciente.nombre
        }
    }

# ============================================
# ENDPOINTS - DERIVACIÓN ENTRE HOSPITALES
# ============================================

@app.post("/hospitales/{hospital_id}/pacientes/{paciente_id}/derivar")
async def derivar_paciente(
    hospital_id: str,
    paciente_id: str,
    derivacion_data: DerivarPacienteRequest,
    session: Session = Depends(get_session)
):
    """✅ CORREGIDO: Paciente queda en estado pendiente hasta ser aceptado"""
    paciente = session.get(Paciente, paciente_id)
    
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    
    if paciente.hospital_id != hospital_id:
        raise HTTPException(status_code=400, detail="Paciente no pertenece al hospital")
    
    # Verificar que el hospital destino existe
    hospital_destino = session.get(Hospital, derivacion_data.hospital_destino_id)
    if not hospital_destino:
        raise HTTPException(status_code=404, detail=f"Hospital destino {derivacion_data.hospital_destino_id} no encontrado")
    
    if derivacion_data.hospital_destino_id == hospital_id:
        raise HTTPException(status_code=400, detail="No se puede derivar al mismo hospital")
    
    # Guardar información de origen
    paciente.hospital_origen_id = hospital_id
    paciente.motivo_derivacion = derivacion_data.motivo_derivacion
    paciente.derivacion_pendiente = True  # ✅ CRÍTICO: Mantener en TRUE
    
    # Si tiene cama asignada, marcarla EN_TRASLADO (NO liberarla hasta confirmar)
    if paciente.cama_id:
        cama_origen = session.get(Cama, paciente.cama_id)
    if cama_origen:
        paciente.cama_origen_id = paciente.cama_id  # Guardar para posible rechazo
        cama_origen.estado = EstadoCamaEnum.EN_TRASLADO
        # ✅ CRÍTICO: NO limpiar paciente_id - necesario para vincular en dashboard
        # cama_origen.paciente_id ya tiene el valor correcto, no modificar
        session.add(cama_origen)
        print(f"🚑 Derivación: Cama origen {cama_origen.id} → EN_TRASLADO con paciente_id={cama_origen.paciente_id}")
    
    # ✅ CORRECCIÓN: Cambiar al hospital destino pero NO ponerlo en lista de espera normal
    paciente.hospital_id = derivacion_data.hospital_destino_id
    paciente.cama_id = None
    paciente.cama_destino_id = None
    paciente.en_espera = False  # ✅ FALSE porque está pendiente de aceptación, no en espera normal
    
    session.add(paciente)
    session.commit()
    session.refresh(paciente)
    
    # Notificar ambos hospitales
    await notificar_cambio(
        hospital_id=hospital_id,
        evento="paciente_derivado_salida",
        session=session,
        detalles={
            "paciente_id": paciente.id,
            "paciente_nombre": paciente.nombre,
            "hospital_destino": hospital_destino.nombre,
            "motivo": derivacion_data.motivo_derivacion
        }
    )
    
    await notificar_cambio(
        hospital_id=derivacion_data.hospital_destino_id,
        evento="paciente_derivado_entrada",
        session=session,
        detalles={
            "paciente_id": paciente.id,
            "paciente_nombre": paciente.nombre,
            "hospital_origen": hospital_id,
            "motivo": derivacion_data.motivo_derivacion
        }
    )
    
    print(f"✅ Paciente {paciente.nombre} derivado de {hospital_id} a {derivacion_data.hospital_destino_id}")
    
    return {
        "mensaje": f"Paciente derivado exitosamente a {hospital_destino.nombre}",
        "paciente": paciente,
        "hospital_destino": hospital_destino.nombre,
        "pendiente_aceptacion": True
    }


@app.post("/hospitales/{hospital_id}/pacientes/{paciente_id}/aceptar-derivacion")
async def aceptar_derivacion(
    hospital_id: str,
    paciente_id: str,
    session: Session = Depends(get_session)
):
    """
    Acepta un paciente derivado y le asigna cama automáticamente.
    ✅ NO libera la cama origen - espera confirmación de egreso manual.
    """
    from logic import asignar_cama
    
    paciente = session.get(Paciente, paciente_id)
    
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    
    if paciente.hospital_id != hospital_id:
        raise HTTPException(status_code=400, detail="Paciente no pertenece a este hospital")
    
    if not paciente.derivacion_pendiente:
        raise HTTPException(status_code=400, detail="El paciente no tiene derivación pendiente")
    
    # Buscar cama disponible automáticamente
    query_camas = select(Cama).where(
        Cama.hospital_id == hospital_id,
        Cama.estado == EstadoCamaEnum.LIBRE
    )
    camas_disponibles = session.exec(query_camas).all()
    
    if not camas_disponibles:
        raise HTTPException(
            status_code=400, 
            detail="No hay camas disponibles. Rechaza la derivación o espera a que se libere una cama."
        )
    
    # Obtener TODAS las camas del hospital para la lógica de asignación
    query_todas_camas = select(Cama).where(Cama.hospital_id == hospital_id)
    todas_camas = session.exec(query_todas_camas).all()
    
    # Asignar cama automáticamente
    cama_asignada = asignar_cama(paciente, list(camas_disponibles), todas_camas, session)
    
    if not cama_asignada:
        raise HTTPException(
            status_code=400,
            detail="No se encontró cama adecuada para los requerimientos del paciente"
        )
    
    # Confirmar asignación
    cama_asignada.estado = EstadoCamaEnum.PENDIENTE_TRASLADO
    cama_asignada.paciente_id = paciente.id
    paciente.cama_destino_id = cama_asignada.id
    paciente.derivacion_pendiente = False
    paciente.en_espera = True  # ✅ En espera de llegar físicamente
    
     # Marcar la cama origen como EN_TRASLADO para indicar que el paciente está en proceso de traslado
    if paciente.cama_origen_id:
        cama_origen = session.get(Cama, paciente.cama_origen_id)
        if cama_origen:
            # ✅ PRIMERO: Asegurar que paciente_id esté presente ANTES de cambiar estado
            if not cama_origen.paciente_id:
                print(f"⚠️ ADVERTENCIA: cama_origen.paciente_id es None, restaurando a {paciente.id}")
                cama_origen.paciente_id = paciente.id
            
            # ✅ SEGUNDO: Mantener la cama como EN_TRASLADO (naranja) hasta que se confirme el egreso
            cama_origen.estado = EstadoCamaEnum.EN_TRASLADO
            
            session.add(cama_origen)
            session.commit()  # ✅ Commit para asegurar que se guarda
            session.refresh(cama_origen)  # ✅ Refresh para verificar
            
            # 🔍 LOGS DE DEBUG CRÍTICOS para verificar estado
            print(f"\n{'='*60}")
            print(f"🚑 DERIVACIÓN ACEPTADA - Estado del Sistema:")
            print(f"{'='*60}")
            print(f"📋 PACIENTE:")
            print(f"   - Nombre: {paciente.nombre}")
            print(f"   - ID: {paciente.id}")
            print(f"   - Hospital actual: {paciente.hospital_id}")
            print(f"   - Hospital origen: {paciente.hospital_origen_id}")
            print(f"   - cama_origen_id: {paciente.cama_origen_id}")
            print(f"   - cama_destino_id: {paciente.cama_destino_id}")
            print(f"   - derivacion_pendiente: {paciente.derivacion_pendiente}")
            print(f"   - egreso_confirmado: {paciente.egreso_confirmado}")
            print(f"\n🛏️ CAMA ORIGEN ({cama_origen.id}):")
            print(f"   - Estado: {cama_origen.estado.value}")
            print(f"   - paciente_id: {cama_origen.paciente_id}")
            print(f"   - Hospital: {cama_origen.hospital_id}")
            print(f"\n✅ VERIFICACIÓN:")
            print(f"   - paciente_id está presente: {'✅ SÍ' if cama_origen.paciente_id else '❌ NO'}")
            print(f"   - Estado es EN_TRASLADO: {'✅ SÍ' if cama_origen.estado == EstadoCamaEnum.EN_TRASLADO else '❌ NO'}")
            print(f"\n💡 La cama origen debe aparecer NARANJA con botón 'Confirmar Egreso' en {paciente.hospital_origen_id}")
            print(f"{'='*60}\n")
        
        # ✅ CRÍTICO: Verificar y restaurar paciente_id si se limpió
        if not cama_origen.paciente_id:
            # Si se perdió el ID, restaurarlo desde el paciente
            cama_origen.paciente_id = paciente.id
            print(f"⚠️ RESTAURANDO paciente_id en cama origen {cama_origen.id}")
        
        session.add(cama_origen)
        print(f"🚑 Aceptación: Cama origen {cama_origen.id} EN_TRASLADO, paciente_id={cama_origen.paciente_id}")
        print(f"   Hospital origen: {paciente.hospital_origen_id}")
        print(f"   Paciente: {paciente.nombre} (ID: {paciente.id})")
    
    # Actualizar sexo de sala en la nueva cama
    from logic import actualizar_sexo_sala
    query_todas_destino = select(Cama).where(Cama.hospital_id == hospital_id)
    todas_camas_destino = session.exec(query_todas_destino).all()
    actualizar_sexo_sala(cama_asignada, paciente, todas_camas_destino, session)
    
    session.add(cama_asignada)
    session.add(paciente)
    session.commit()
    session.refresh(paciente)
    session.refresh(cama_asignada)
    
    # Notificar ambos hospitales
    await notificar_cambio(
        hospital_id=hospital_id,
        evento="derivacion_aceptada",
        session=session,
        detalles={
            "paciente_id": paciente.id,
            "paciente_nombre": paciente.nombre,
            "cama_asignada": cama_asignada.id
        }
    )
    
    # ✅ NUEVO: Notificar al hospital origen que puede confirmar egreso
    if paciente.hospital_origen_id:
        await notificar_cambio(
            hospital_id=paciente.hospital_origen_id,
            evento="derivacion_aceptada_confirmar_egreso",
            session=session,
            detalles={
                "paciente_id": paciente.id,
                "paciente_nombre": paciente.nombre,
                "hospital_destino": hospital_id,
                "cama_origen_id": paciente.cama_origen_id
            }
        )
    
    print(f"✅ Derivación aceptada: {paciente.nombre} → {cama_asignada.id}")
    
    return {
        "mensaje": f"Derivación aceptada. Paciente asignado a cama {cama_asignada.id}",
        "paciente_id": paciente.id,
        "accion": "aceptado",
        "hospital_actual_id": hospital_id,
        "cama_asignada": jsonable_encoder(cama_asignada)
    }


@app.post("/hospitales/{hospital_id}/pacientes/{paciente_id}/rechazar-derivacion")
async def rechazar_derivacion(
    hospital_id: str,
    paciente_id: str,
    request_data: RechazarDerivacionRequest,
    session: Session = Depends(get_session)
):
    """
    Rechaza un paciente derivado y lo devuelve al hospital de origen.
    """
    paciente = session.get(Paciente, paciente_id)
    
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    
    if paciente.hospital_id != hospital_id:
        raise HTTPException(status_code=400, detail="Paciente no pertenece a este hospital")
    
    if not paciente.derivacion_pendiente:
        raise HTTPException(status_code=400, detail="El paciente no tiene derivación pendiente")
    
    if not paciente.hospital_origen_id:
        raise HTTPException(status_code=400, detail="No se puede determinar el hospital de origen")
    
    hospital_origen_id = paciente.hospital_origen_id
    
    # Guardar motivo de rechazo
    paciente.motivo_rechazo_derivacion = request_data.motivo_rechazo
    
    # Devolver al hospital de origen
    paciente.hospital_id = hospital_origen_id
    paciente.derivacion_pendiente = False
    
    # Si tenía cama origen, intentar restaurarla
    if paciente.cama_origen_id:
        cama_origen = session.get(Cama, paciente.cama_origen_id)
        # Verificar si la cama está EN_TRASLADO (esperando confirmación)
        if cama_origen and cama_origen.estado == EstadoCamaEnum.EN_TRASLADO:
            # La cama estaba reservada, restaurar a OCUPADA
            cama_origen.estado = EstadoCamaEnum.OCUPADA
            cama_origen.paciente_id = paciente.id  # Ya estaba asignado
            paciente.cama_id = cama_origen.id
            paciente.en_espera = False
            session.add(cama_origen)
            mensaje = f"Derivación rechazada. Paciente devuelto a {cama_origen.id} en hospital de origen"
        elif cama_origen and cama_origen.estado == EstadoCamaEnum.OCUPADA:
            # La cama fue ocupada por otro paciente (poco probable)
            paciente.cama_id = None
            paciente.en_espera = True
            mensaje = "Derivación rechazada. Cama original ocupada, paciente en lista de espera"
        else:
            # La cama ya fue ocupada o liberada, va a lista de espera
            paciente.cama_id = None
            paciente.en_espera = True
            mensaje = "Derivación rechazada. Paciente devuelto a lista de espera del hospital de origen"
    else:
        # No tenía cama, volver a lista de espera
        paciente.cama_id = None
        paciente.en_espera = True
        mensaje = "Derivación rechazada. Paciente devuelto a lista de espera del hospital de origen"
    
    # Limpiar campos de derivación (mantener motivo_derivacion y motivo_rechazo para historial)
    paciente.hospital_origen_id = None
    paciente.cama_origen_id = None
    # NO limpiar motivo_derivacion ni motivo_rechazo_derivacion para mantener historial
    
    session.add(paciente)
    session.commit()
    session.refresh(paciente)
    
    # Notificar ambos hospitales
    await notificar_cambio(
        hospital_id=hospital_id,
        evento="derivacion_rechazada",
        session=session,
        detalles={
            "paciente_id": paciente.id,
            "paciente_nombre": paciente.nombre
        }
    )
    
    await notificar_cambio(
        hospital_id=hospital_origen_id,
        evento="derivacion_rechazada_notificacion",
        session=session,
        detalles={
            "paciente_id": paciente.id,
            "paciente_nombre": paciente.nombre,
            "volvio_a_cama": paciente.cama_id is not None
        }
    )
    
    print(f"✅ {mensaje}")
    
    return {
        "mensaje": mensaje,
        "paciente_id": paciente.id,
        "accion": "rechazado",
        "hospital_actual_id": hospital_origen_id,
        "en_espera": paciente.en_espera,
        "cama_actual": paciente.cama_id
    }


@app.post("/hospitales/{hospital_id}/pacientes/{paciente_id}/confirmar-egreso")
async def confirmar_egreso(
    hospital_id: str,
    paciente_id: str,
    session: Session = Depends(get_session)
):
    """
    Confirma el egreso del paciente del hospital de origen después de que fue aceptado en otro hospital.
    Libera la cama origen.
    """
    # Buscar el paciente en el hospital de origen
    paciente = session.exec(
        select(Paciente).where(
            Paciente.id == paciente_id,
            Paciente.hospital_origen_id == hospital_id  # El hospital actual debe ser el origen
        )
    ).first()
    
    if not paciente:
        raise HTTPException(
            status_code=404, 
            detail="Paciente no encontrado o no fue derivado desde este hospital"
        )
    
    if paciente.egreso_confirmado:
        raise HTTPException(status_code=400, detail="El egreso ya fue confirmado")
    
    if not paciente.cama_origen_id:
        raise HTTPException(status_code=400, detail="No hay cama origen para liberar")
    
    # Liberar la cama origen
    cama_origen = session.get(Cama, paciente.cama_origen_id)
    if not cama_origen:
        raise HTTPException(status_code=404, detail="Cama origen no encontrada")
    
    if cama_origen.hospital_id != hospital_id:
        raise HTTPException(status_code=400, detail="La cama no pertenece a este hospital")
    
    # Obtener todas las camas para actualizar sala compartida
    query_todas = select(Cama).where(Cama.hospital_id == hospital_id)
    todas_camas = session.exec(query_todas).all()
    
    # Liberar sala compartida si aplica
    liberar_sexo_sala(cama_origen, todas_camas, session)
    
    # Liberar la cama
    cama_origen.estado = EstadoCamaEnum.LIBRE
    cama_origen.paciente_id = None
    session.add(cama_origen)
    
    # Marcar egreso como confirmado
    paciente.egreso_confirmado = True
    paciente.cama_origen_id = None  # Ya no tiene cama origen
    session.add(paciente)
    
    session.commit()
    session.refresh(cama_origen)
    
    await notificar_cambio(
        hospital_id=hospital_id,
        evento="egreso_confirmado",
        session=session,
        detalles={
            "paciente_id": paciente.id,
            "paciente_nombre": paciente.nombre,
            "cama_liberada": cama_origen.id
        }
    )
    
    print(f"✅ Egreso confirmado: {paciente.nombre} - Cama {cama_origen.id} liberada")
    
    return {
        "mensaje": f"Egreso confirmado. Cama {cama_origen.id} liberada",
        "paciente_id": paciente.id,
        "cama_liberada": cama_origen.id
    }


# ============================================
# ENDPOINTS - ALTAS
# ============================================

@app.post("/hospitales/{hospital_id}/camas/{cama_id}/alta")
async def confirmar_alta(
    hospital_id: str,
    cama_id: str,
    session: Session = Depends(get_session)
):
    """Confirma el alta de un paciente y libera la cama."""
    cama = session.get(Cama, cama_id)
    
    if not cama:
        raise HTTPException(status_code=404, detail="Cama no encontrada")
    
    if cama.hospital_id != hospital_id:
        raise HTTPException(status_code=400, detail="Cama no pertenece al hospital")
    
    if not cama.paciente_id:
        raise HTTPException(status_code=400, detail="No hay paciente asignado a esta cama")
    
    paciente = session.get(Paciente, cama.paciente_id)
    paciente_nombre = paciente.nombre if paciente else "Desconocido"
    paciente_id = cama.paciente_id
    
    # Liberar sexo de sala si es compartida
    query_todas = select(Cama).where(Cama.hospital_id == hospital_id)
    todas_camas = session.exec(query_todas).all()
    liberar_sexo_sala(cama, todas_camas, session)
    
    cama.estado = EstadoCamaEnum.LIBRE
    cama.paciente_id = None
    
    if paciente:
        paciente.cama_id = None
        session.add(paciente)
    
    session.add(cama)
    session.commit()
    session.refresh(cama)
    
    await notificar_cambio(
        hospital_id=hospital_id,
        evento="alta_confirmada",
        session=session,
        detalles={
            "cama_id": cama.id,
            "paciente_id": paciente_id,
            "paciente_nombre": paciente_nombre
        }
    )
    
    print(f"✅ Alta confirmada: {paciente_nombre} - Cama {cama_id} liberada")
    
    return {
        "mensaje": f"Alta confirmada para {paciente_nombre}",
        "cama_id": cama.id,
        "estado": cama.estado
    }


@app.post("/hospitales/{hospital_id}/camas/{cama_id}/sugerir-alta")
async def sugerir_alta(
    hospital_id: str,
    cama_id: str,
    session: Session = Depends(get_session)
):
    """Marca una cama como candidata para alta."""
    cama = session.get(Cama, cama_id)
    
    if not cama:
        raise HTTPException(status_code=404, detail="Cama no encontrada")
    
    if cama.hospital_id != hospital_id:
        raise HTTPException(status_code=400, detail="Cama no pertenece al hospital")
    
    if cama.estado != EstadoCamaEnum.OCUPADA:
        raise HTTPException(status_code=400, detail="Solo se puede sugerir alta en camas ocupadas")
    
    cama.estado = EstadoCamaEnum.ALTA_SUGERIDA
    session.add(cama)
    session.commit()
    session.refresh(cama)
    
    await notificar_cambio(
        hospital_id=hospital_id,
        evento="alta_sugerida",
        session=session,
        detalles={
            "cama_id": cama.id,
            "paciente_id": cama.paciente_id
        }
    )
    
    return {
        "mensaje": "Alta sugerida para esta cama",
        "cama_id": cama.id,
        "estado": cama.estado
    }


@app.post("/hospitales/{hospital_id}/camas/{cama_id}/cancelar-alta")
async def cancelar_alta(
    hospital_id: str,
    cama_id: str,
    session: Session = Depends(get_session)
):
    """Cancela o revierte una alta sugerida, devolviendo la cama al estado OCUPADA."""
    cama = session.get(Cama, cama_id)
    
    if not cama:
        raise HTTPException(status_code=404, detail="Cama no encontrada")
    
    if cama.hospital_id != hospital_id:
        raise HTTPException(status_code=400, detail="Cama no pertenece al hospital")
    
    if cama.estado != EstadoCamaEnum.ALTA_SUGERIDA:
        raise HTTPException(status_code=400, detail="Solo se puede cancelar alta en camas con alta sugerida")
    
    if not cama.paciente_id:
        raise HTTPException(status_code=400, detail="No hay paciente asignado a esta cama")
    
    # Revertir al estado OCUPADA
    cama.estado = EstadoCamaEnum.OCUPADA
    session.add(cama)
    session.commit()
    session.refresh(cama)
    
    await notificar_cambio(
        hospital_id=hospital_id,
        evento="alta_cancelada",
        session=session,
        detalles={
            "cama_id": cama.id,
            "paciente_id": cama.paciente_id
        }
    )
    
    paciente = session.get(Paciente, cama.paciente_id)
    paciente_nombre = paciente.nombre if paciente else "Desconocido"
    
    return {
        "mensaje": f"Alta cancelada para {paciente_nombre}. Cama devuelta a estado OCUPADA.",
        "cama_id": cama.id,
        "estado": cama.estado
    }


# ============================================
# ENDPOINTS - ESTADÍSTICAS
# ============================================

@app.get("/hospitales/{hospital_id}/estadisticas")
def obtener_estadisticas(
    hospital_id: str,
    session: Session = Depends(get_session)
):
    """Obtiene estadísticas detalladas de ocupación del hospital."""
    hospital = session.get(Hospital, hospital_id)
    if not hospital:
        raise HTTPException(status_code=404, detail=f"Hospital {hospital_id} no encontrado")
    
    query_camas = select(Cama).where(Cama.hospital_id == hospital_id)
    todas_camas = session.exec(query_camas).all()
    
    if not todas_camas:
        return {"mensaje": "Hospital sin camas configuradas"}
    
    estadisticas = {
        "total_camas": len(todas_camas),
        "por_estado": {},
        "por_servicio": {}
    }
    
    for estado in EstadoCamaEnum:
        count = len([c for c in todas_camas if c.estado == estado])
        estadisticas["por_estado"][estado.value] = count
    
    for servicio in ServicioEnum:
        camas_servicio = [c for c in todas_camas if c.servicio == servicio]
        estadisticas["por_servicio"][servicio.value] = {
            "total": len(camas_servicio),
            "libres": len([c for c in camas_servicio if c.estado == EstadoCamaEnum.LIBRE]),
            "ocupadas": len([c for c in camas_servicio if c.estado == EstadoCamaEnum.OCUPADA]),
            "tasa_ocupacion": round(
                (len([c for c in camas_servicio if c.estado == EstadoCamaEnum.OCUPADA]) / len(camas_servicio) * 100) if camas_servicio else 0,
                2
            )
        }
    
    camas_ocupadas = len([c for c in todas_camas if c.estado == EstadoCamaEnum.OCUPADA])
    estadisticas["tasa_ocupacion"] = round((camas_ocupadas / len(todas_camas)) * 100, 2)
    
    query_espera = select(Paciente).where(
        Paciente.hospital_id == hospital_id,
        Paciente.en_espera == True,
        Paciente.cama_id == None  # Solo pacientes sin cama confirmada
    )
    pacientes_espera = session.exec(query_espera).all()
    estadisticas["pacientes_en_espera"] = len(pacientes_espera)
    
    return estadisticas


# ============================================
# WEBSOCKET ENDPOINT
# ============================================

@app.websocket("/ws/{hospital_id}")
async def websocket_endpoint(websocket: WebSocket, hospital_id: str):
    """WebSocket para actualizaciones en tiempo real del hospital."""
    await manager.connect(websocket, hospital_id)
    
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(websocket, hospital_id)
    except Exception as e:
        print(f"❌ Error en WebSocket: {e}")
        manager.disconnect(websocket, hospital_id)


# ============================================
# ENDPOINTS DE TESTING Y SALUD
# ============================================

@app.get("/health")
def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "redis_available": redis_client is not None,
        "timestamp": datetime.utcnow().isoformat()
    }


@app.post("/test/redis")
async def test_redis(mensaje: str = "Test"):
    """Endpoint de prueba para verificar la conexión a Redis."""
    await enviar_evento(
        tipo="test_evento",
        datos={
            "mensaje": mensaje,
            "test": True
        }
    )
    
    return {
        "mensaje": "Evento de prueba enviado" if redis_client else "Redis no disponible",
        "redis_status": "connected" if redis_client else "disconnected"
    }


# ============================================
# MAIN
# ============================================

if __name__ == "__main__":
    import uvicorn
    
    print("\n" + "="*60)
    print("🏥 SISTEMA DE GESTIÓN DE CAMAS HOSPITALARIAS")
    print("="*60)
    print("\n📋 INSTRUCCIONES:")
    print("   1. Servidor: http://localhost:8000")
    print("   2. Dashboard: http://localhost:8000/dashboard")
    print("   3. API Docs: http://localhost:8000/docs")
    print("\n🔧 PRIMER USO:")
    print("   python setup_hospital.py")
    print("\n")
    
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")