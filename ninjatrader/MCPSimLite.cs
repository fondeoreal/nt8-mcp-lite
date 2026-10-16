#region Using declarations
using System;
using System.Collections.Generic;
#endregion

// =====================================================================
//  MCPSimLite.cs  -  Sustituto de MCPSim.cs en la edicion Lite del NT8-MCP.
//
//  MCPBridge.cs es una clase parcial que espera SimVersion, SimCommands y
//  DispatchSim. En la Lite no existe el modo simulador: no hay ningun comando
//  de escritura y cualquier intento devuelve un error. Este fichero NO debe
//  convivir con MCPSim.cs (el instalador borra uno al instalar el otro).
// =====================================================================

namespace NinjaTrader.NinjaScript.AddOns
{
    public partial class MCPBridge
    {
        public const string SimVersion = "lite";

        private static readonly HashSet<string> SimCommands = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

        private object DispatchSim(string cmd, Dictionary<string, object> a)
        {
            throw new InvalidOperationException("El modo simulador solo existe en la edicion Pro del NT8-MCP.");
        }
    }
}
