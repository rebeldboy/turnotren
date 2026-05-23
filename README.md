# TurnoTren

Datos públicos para la app personal TurnoTren.

Este repositorio genera automáticamente `turnotren.json` usando:

- GTFS estático de Renfe Cercanías.
- `trip_updates.json`.
- `vehicle_positions.json`.
- `alerts.json`.

La app Android debe leer:

```text
https://rebeldboy.github.io/turnotren/turnotren.json
```

Regla de seguridad: si no hay dato real validado, la app debe mostrar `SIN DATO` y no inventar horarios.
