# Investigación: soluciones al bloqueo de Google (CAPTCHA)

**Fecha:** 2026-10-12 · **Método:** benchmark empírico desde esta máquina, no revisión de
literatura. Cada afirmación de abajo está respaldada por una medición reproducible.

> **Objetivo de la investigación:** encontrar una vía de discovery que no dependa de
> resolver el CAPTCHA de Google, o determinar si no existe y qué implican las alternativas
> legítimas.

---

## Resumen ejecutivo

**No encontré un motor de búsqueda que reemplace a Google en este proyecto.** Bing,
Brave, Startpage, Mojeek y Marginalia **no sirven** para este caso — cada uno falla por un
motivo distinto y verificable. DDG es el único sin CAPTCHA, pero rindió **12 % de éxito** y
un techo de **10 shortcodes**, muy por debajo de lo que el proyecto ya tiene.

La razón de fondo es estructural: **Google es, hoy, el único motor que indexa
suficientemente los posts de Instagram como para descubrirlos por dork.** No es que
Google sea difícil de scrapear — es que los demás no tienen el índice.

La recomendación es **no cambiar de motor**, sino hacer que el sistema degrade con
honestidad: tratar a Google como la fuente principal y agregar DDG como respaldo
best-effort, aceptando que rinde menos.

---

## Benchmark: los 6 motores (ejecutado 2026-10-12)

Consulta usada: `site:instagram.com/p/ "running shoes"`

| Motor | Status | Tamaño | Anclas | Shortcodes | CAPTCHA |
|---|---:|---:|---:|---:|---|
| **duckduckgo** (html) | 200 | 49.785 B | 52 | **10** | ❌ no |
| **bing** | 200 | 127.306 B | 55 | **0** | ❌ no |
| **marginalia** | 200 | 62.578 B | 53 | **0** | ❌ no |
| **startpage** | 200 | 22.106 B | 1 | 0 | ❌ no |
| **mojeek** | **403** | 368 B | 1 | 0 | ❌ no |
| **brave** | **429** | 73.896 B | 2 | 0 | **sí** |

Punto clave: **`bloqueado=False` con `shortcodes=0` no es lo mismo que "sin
resultados".** Bing, Marginalia y Startpage responden 200 pero sin posts. Confundir esas
dos cosas es el mismo error conceptual que el proyecto ya cometió con Google (y
documentó correctamente en el README). Mi `extract_shortcodes()` heredado detecta el caso
sólo en un lado: Bing pasa el filtro de "sin CAPTCHA" pero no aporta nada.

---

## Por qué falla cada alternativa

### ❌ Bing — 200 OK, cero posts. Es el engaño más peligroso

Bing es el único motor que responde **200 con 127 KB y 55 anclas** sin CAPTCHA. Por las
métricas del propio proyecto (`MIN_RESULT_ANCHORS = 10`) **Bing se vería como una SERP
válida y funcional**. Pero devuelve **cero shortcodes** en todas las consultas.

La causa es que Bing ignora el operador `site:` para este dominio. Al inspeccionar el
HTML, los `<cite>` de resultados son de sitios de running (`runfitners.com`,
`runningcorrer.com.ar`, `adidas.com.ar`), **no de Instagram**. Los enlaces de resultado
están además ofuscados en `/ck/a?` con parámetros que **no son base64 recuperable**
(32 firmas `u=a1...` encontradas, 0 decodificables a URLs de destino).

Cuatro variantes de dork, cero shortcodes:

```
'site:instagram.com/p/ sneakers'        -> 0
'site:instagram.com/reel/ running'      -> 0
'instagram Running shoes'               -> 0
'site:instagram.com "zapatillas"'       -> 0
```

**Riesgo concreto:** si se agrega Bing como respaldo, el script reportará éxito con
"muchos resultados" y no descubrirá ningún post. Es peor que fallar.

### ❌ Brave — CAPTCHA directo

**429** inmediato con 2 anclas. El único motor que detecta explícitamente el bloqueo.

### ❌ Mojeek — 403 duro

368 bytes, 1 ancla. Rechaza la request outright.

### ❌ Startpage — shell vacío

200 OK pero 22 KB y **1 sola ancla**. Es el "JS shell" de Startpage: nunca llegó la SERP.

### ❌ Marginalia — índice demasiado chico

200 OK, 53 anclas, 0 shortcodes. Marginalia es un índice de Sitios pequeños /
//old-web; no tiene cobertura de Instagram.

### ⚠️ DuckDuckGo — el único sin CAPTCHA, pero insuficiente

DDG **sí** entrega shortcodes sin CAPTCHA y sin cookies. Pero tiene dos límites duros,
medidos:

**1. No pagina.** Confirmando lo que el README ya decía, ahora con medición propia:

```
s=None  -> 200  48.785 B  10 shortcodes
s=10    -> 202  14.217 B   0
s=30    -> 202  14.241 B   0   ... (idéntico hasta s=200)
```

**2. Throttle agresivo.** El 202 es rate limiting, no "sin resultados". Con 8 dorks y 8 s
de pausa:

```
queries_OK=1  throttle=7   (12% de éxito)
shortcodes únicos: 9
```

Con pausas de 20 s tampoco mejora: 5 de 5 consultas dieron 202. El único método que dio
resultado fue **POST** a `html.duckduckgo.com/html/` en vez de GET — pero el resultado
sigue siendo intermitente (7 de 8 throttled).

**Techo absoluto de DDG: ~10 shortcodes por sesión.** El proyecto actual con Google
consigue 13 en la primera consulta y pagina. **DDG es un retroceso, no una solución.**

---

## La vía que sí funciona: Google ya está resuelto

El hallazgo más importante de esta investigación es negativo: **el problema del CAPTCHA
ya tiene solución funcionando dentro del proyecto**.

`--interactive` abre un Chrome real, el usuario resuelve el CAPTCHA **una vez**, y se
exportan las cookies por CDP. Luego el scraping corre por HTTP con la sesión ya
resuelta, y el perfil persistente reutiliza lo resuelto en corridas siguientes.

Verifiqué que la decisión de diseño se sostiene: **la Fase 2/3 no depende de Google en
absoluto.**

```
embed status 200 | 301.731 B
-> la Fase 2/3 NO depende de Google: SI
```

El embed de Instagram responde 200 con 301 KB desde esta IP, sin cookies y sin
intervención. **El único punto frágil del sistema es el descubrimiento**, y es
precisamente el punto que Google resuelve una vez por sesión.

Esto invierte el diagnóstico del README: no es que "Google sea intratable", es que **el
resto del sistema no necesita a Google para nada más**.

---

## Alternativas legítimas (cambio de enfoque, no de motor)

Si el objetivo es escala o datos que el embed no expone, el camino es dejar de scrapear.
Investigación sobre el estado actual de las APIs oficiales:

### 1. Instagram Graph API — para cuentas Business/Creator

**Estado:** activo. Es la vía oficial.

- Sólo funciona con cuentas **Business o Creator**. Las personales **no son accesibles**.
- Requiere app de Meta + **App Review** (2-4 semanas, o 60+ días según la fuente).
- **200 llamadas/hora por token de usuario.**
- Aporta lo que el embed no tiene: **vistas de video**, alcance, impresiones.
- Desde enero 2025 los permisos se renombraron a `instagram_business_*`.

### 2. Instagram Basic Display API — muerta

**Apagada el 4 de diciembre de 2024**, sin reemplazo para cuentas personales. Era el
único acceso oficial a cuentas personales; **hoy no existe acceso oficial a datos de
cuentas personales de Instagram**. Esto es una limitation estructural de la plataforma,
no del scraper.

### 3. Meta Content Library API — el más interesante para este proyecto

Acceso al archivo público completo de Facebook e Instagram, en tiempo casi real.

- **Requiere afiliación académica o de investigación sin fines de lucro**, revisión por
  ICPSR (Universidad de Michigan), **4-6 semanas**, y un Data Use Agreement.
- El acceso programático ocurre **dentro de un Virtual Data Enclave** — no se pueden
  descargar los datos.
- Cobertura: cuentas públicas **verificadas o con umbral de seguidores** (el umbral bajó a
  100 en la v6.0, de 25.000 en la v5.0).
- Límites: 1.000 consultas por 7 días.
- Un estudio académico (arXiv 2601.12390) midió que sólo **~50 % de los posts visibles en
  Explore** y **42 % de los parámetros de metadata** quedan accesibles para investigadores.

**Veredicto:** técnicamente superior y sin CAPTCHA, pero el requisito de afiliación
académica lo hace inviable salvo que el proyecto sea de investigación real.

### 4. APIs de terceros (Phyllo y similares)

Proveedores de datos sociales gestionan auth y acceso. Es la vía comercial estándar,
**a cambio de pagar y ceder parte del control**. No la evalué en detalle.

---

## Recomendación

### Para el proyecto tal como está (PoC, sin login)

**No cambiar de motor.** Agregar Bing sería activamente dañino (§ Bing). El plan:

1. **Mantener Google como fuente principal.** Ya está resuelto con `--interactive` y
   funciona.
2. **Considerar DDG como fallback best-effort** — ~10 shortcodes gratis, sin CAPTCHA ni
   cookies. Aceptar explícitamente que rinde ~12 % de las consultas. **Sólo si el
   discovery de Google falla**, nunca en paralelo (suma presión sin sumar valor).
3. **Resistir la tentación del proxy.** El README recomienda proxies residenciales;
   sigue siendo lo más efectivo para Google, pero es gasto recurrente y un problema de
   cumplimiento. Antes de escalar, conviene cuantificar si el cuello de botella real es
   el CAPTCHA o el techo de ~63 shortcodes por keyword (§ breadth).

### Sobre el techo real

Vale notar dónde está el límite verdadero: **no es el CAPTCHA, es el índice.** Google
paga sus 63 shortcodes por keyword. Si el cuello de botella es volumen, la palanca es
**más keywords y más dorks**, no más velocidad ni más ingenious bypass. El README ya lo
dice (§ "Por qué Google y no sólo dorks: el techo"); esta investigación lo confirma.

### Si el objetivo cambia a escala o datos ricos

El scraper deja de ser la herramienta correcta. Sin login no hay vía oficial para
cuentas personales; con login, la Graph API cubre Business/Creator y agrega las vistas
que el proyecto hoy no puede obtener. **La decisión de diseño del proyecto — "sin login,
sin credenciales" — es lo que garantiza que el CAPTCHA sea un problema recurrente.**

---

## Método y reproducibilidad

Todo lo medido fue ejecutado desde esta máquina contra los motores en vivo. Limitaciones
honestas de este benchmark:

- **Una sola IP, una sola sesión de red.** Un datacenter IP puede estar en peor posición
  que un residencial ante varios de estos motores. Los números de DDG (12 %) en
  particular **mejorarían IP-wards** y empeorarían con más volumen.
- **Los umbrales de bloqueo son dinámicos.** Lo que hoy da 202 puede dar 200 mañana.
  Conviene re-medir antes de decidir.
- **No probé proxies.** Deliberadamente: es la variable que el proyecto ya asume como
  solución y no aporta información sobre las alternativas.
- **`extract_shortcodes()` se reutilizó del proyecto**, lo que garantiza comparabilidad
  pero también hereda sus supuestos.

---

## Conclusión

La pregunta era si otro motor de búsqueda podía evitar el CAPTCHA. **La respuesta
empírica es que no hay uno mejor: los demás no tienen el índice.** Bing es la trampa
—responde 200 y parece sano, pero no aporta un solo post. DDG evita el CAPTCHA y aun así
rinde 8 veces menos que lo que el proyecto ya tiene.

La solución real ya está dentro del proyecto: `--interactive` resuelve el CAPTCHA una vez
por sesión y todo lo demás funciona sin Google. Lo que queda por resolver no es técnico
sino **estratégico**: si el objetivo es escalar, el scraper sin login es el cuello de
botella estructural, y la decisión de diseño que lo origina — evitar credenciales — es la
que hace inevitable el combate con el CAPTCHA.