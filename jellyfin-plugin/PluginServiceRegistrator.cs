using Jellyfin.Plugin.MediaPreviewBridge.Markers;
using MediaBrowser.Controller;
using MediaBrowser.Controller.MediaSegments;
using MediaBrowser.Controller.Plugins;
using Microsoft.Extensions.DependencyInjection;

namespace Jellyfin.Plugin.MediaPreviewBridge;

/// <summary>Registers the markers segment provider with Jellyfin's DI container.</summary>
public class PluginServiceRegistrator : IPluginServiceRegistrator
{
    /// <inheritdoc />
    public void RegisterServices(IServiceCollection serviceCollection, IServerApplicationHost applicationHost)
    {
        serviceCollection.AddSingleton<IMediaSegmentProvider, BridgeSegmentProvider>();
    }
}
