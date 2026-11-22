# Motor de Optimización de Donación Renal Pareada (KPD) — v2.0

Prototipo computacional de acceso abierto para la optimización de intercambios en programas de **Donación Renal Pareada (Kidney Paired Donation, KPD)**, calibrado a parámetros epidemiológicos reportados para México.

> **Aviso importante:** Este software es un prototipo de investigación operativa con datos 100% sintéticos y un modelo inmunológico (HLA) simplificado. **No constituye un dispositivo médico** ni debe utilizarse para tomar decisiones clínicas reales.

---

## Características principales

* **Optimización combinatoria exacta (ILP):** Implementa Programación Lineal Entera Binaria mediante `scipy.optimize.milp` (solver HiGHS) y matrices dispersas (`csr_matrix`), compatible con Python 3.12–3.14 sin dependencias propietarias.
* **Ciclos cerrados y cadenas abiertas:**
  * Intercambios simultáneos en ciclos cerrados de 2 y 3 parejas incompatibles ($k \le 3$).
  * Cadenas abiertas tipo NEAD iniciadas por **Donantes Altruistas** (no dirigidos) de hasta $L_{\max}$ eslabones, con cierre hacia la lista de espera general.
* **Priorización clínica ponderada:** Maximiza lexicográficamente el número total de trasplantes efectivos (1,000 puntos base por trasplante) y desempata mediante bonificaciones clínicas por sensibilización (`cPRA >= 80%`, `cPRA >= 98%`), edad pediátrica (`< 18 años`) y compatibilidad HLA.
* **Control de explosión combinatoria:** Poda heurística por escasez de alternativas para escalar a redes nacionales de $N = 1,000$ parejas en menos de 10 segundos.

---

## Instalación y uso

1. Clona este repositorio e instala las dependencias:

    git clone https://github.com/armandocantovar-11y/kpd-mexico-optimizer.git
    cd kpd-mexico-optimizer
    pip install -r requirements.txt

2. Ejecuta la demostración en un pool pequeño (12 parejas + 1 donante altruista):

    python kpd_motor_emparejamiento.py

3. Ejecuta el benchmark nacional comparando hospitales aislados vs. red unificada vs. red con donantes altruistas ($N = 1,000$):

    python benchmark_nacional.py

4. Opciones avanzadas del benchmark:

    # Evaluar impacto marginal agregando 0, 1, 2, 5 y 10 donantes altruistas
    python benchmark_nacional.py --barrido 0,1,2,5,10

    # Configurar tamaño de cadenas, ciclos cerrados y número de altruistas
    python benchmark_nacional.py --altruistas 10 --lmax 4 --kmax 3

---

## Resultados de simulación ($N = 1,000$ parejas, semilla 2026)

Comparación sobre una población sintética calibrada a distribuciones ABO, cPRA y edad pediátrica reportadas en literatura clínica mexicana ($k_{\max}=2$, $L_{\max}=5$, 5 donantes altruistas grupo O):

| Métrica Evaluada | Escenario A: 20 Hospitales Aislados | Escenario B: Red Nacional Única | Escenario C: Red Nacional + 5 Altruistas |
| :--- | :---: | :---: | :---: |
| **Receptores de parejas trasplantados** | 764 (76.4%) | 990 (99.0%) | **999 (99.9%)** |
| **Trasplantes a lista de espera** | 0 | 0 | **5** |
| **Trasplantes totales** | 764 | 990 | **1,004** |
| **Hipersensibilizados ($\text{cPRA} \ge 80\%$)** | 150 / 279 (53.8%) | 272 / 279 (97.5%) | **278 / 279 (99.6%)** |
| **Pediátricos ($< 18$ años)** | 103 / 133 (77.4%) | 129 / 133 (97.0%) | **132 / 133 (99.2%)** |
| **Tiempo de cómputo (laptop estándar)** | 3.92 s | 10.43 s | **9.80 s** |

*Nota metodológica: Los porcentajes absolutos reflejan un catálogo sintético simplificado de 23 antígenos HLA sin crossmatch físico ni restricciones geográficas de traslado. El hallazgo relevante es el efecto relativo de escala (+29.6% global y +81.3% en hipersensibilizados al unificar centros).*

---

## Estructura del repositorio

* `kpd_motor_emparejamiento.py`: Motor base de compatibilidad ABO/HLA, construcción de grafo dirigido (`NetworkX`), enumeración de ciclos/cadenas y solver ILP.
* `benchmark_nacional.py`: Generador poblacional calibrado, poda combinatoria por escasez, autoverificación matemática contra búsqueda exhaustiva y simulación multi-escenario.
* `requirements.txt`: Dependencias mínimas del proyecto.

## Referencias metodológicas

* Abraham, D. J., Blum, A., & Sandholm, T. (2007). *Clearing algorithms for barter exchange markets: Enabling nationwide kidney exchanges*. Proceedings of the 8th ACM Conference on Electronic Commerce.
* Rees, M. A., et al. (2009). *A nonsimultaneous, extended, altruistic-donor chain*. New England Journal of Medicine, 360(11), 1096–1101.