using System;
using MediaBrowser.Common.Configuration;
using MediaBrowser.Common.Plugins;
using MediaBrowser.Model.Plugins;
using MediaBrowser.Model.Serialization;
namespace MarkersLab;
public class PluginConfiguration : BasePluginConfiguration { }
public class Plugin : BasePlugin<PluginConfiguration>
{
    public Plugin(IApplicationPaths p, IXmlSerializer x) : base(p, x) { Instance = this; }
    public static Plugin? Instance { get; private set; }
    public override string Name => "Markers Lab";
    public override Guid Id => Guid.Parse("c2cb9bf9-7c5d-4f1a-9a07-2d6f5e5b00aa");
}
