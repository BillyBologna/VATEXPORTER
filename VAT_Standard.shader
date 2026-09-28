// VAT/Standard  -  Built-in Render Pipeline (surface shader), con GPU instancing.
// Para URP/HDRP usa VAT.hlsl con un Custom Function node en Shader Graph.
//
// Texturas EXR (Import Settings):  sRGB OFF - Generate Mip Maps OFF - Filter Mode = Point -
//   Compression = None - Format = RGBA Float (o RGBA Half si exportaste Float16/Offset)
//   Wrap Mode = Clamp
// Malla: el FBX exportado (..._vat.fbx). Usa UV2 (texcoord1) como lookup.
// Valores de _Frames, _TexW, _Tiles y _Fps: copiarlos de ..._vat.json
//   (frames, texture[0], tiles, fps). _OffsetMode = 1 si "position_mode" es "offset".
Shader "VAT/Standard"
{
    Properties
    {
        _Color ("Color", Color) = (1,1,1,1)
        _MainTex ("Albedo (RGB)", 2D) = "white" {}
        _Glossiness ("Smoothness", Range(0,1)) = 0.5
        _Metallic ("Metallic", Range(0,1)) = 0.0

        [Header(VAT)]
        [NoScaleOffset] _PosTex ("Position texture (EXR)", 2D) = "black" {}
        [NoScaleOffset] _NrmTex ("Normal texture (EXR)", 2D) = "black" {}
        _Frames ("Frames", Float) = 1
        _TexW ("Texture width (px)", Float) = 1
        _Tiles ("Tiles", Float) = 1
        _Fps ("FPS", Float) = 30
        _Speed ("Speed", Float) = 1
        [Toggle] _OffsetMode ("Offset mode (position_mode = offset)", Float) = 0
        _Phase ("Phase in frames (per instance)", Float) = 0
    }
    SubShader
    {
        Tags { "RenderType"="Opaque" }
        LOD 200

        CGPROGRAM
        #pragma surface surf Standard fullforwardshadows vertex:vert addshadow
        #pragma target 3.5

        sampler2D _MainTex;
        sampler2D _PosTex;
        sampler2D _NrmTex;
        fixed4 _Color;
        half _Glossiness;
        half _Metallic;
        float _Frames, _TexW, _Tiles, _Fps, _Speed, _OffsetMode;

        UNITY_INSTANCING_BUFFER_START(Props)
            UNITY_DEFINE_INSTANCED_PROP(float, _Phase)
        UNITY_INSTANCING_BUFFER_END(Props)

        struct Input { float2 uv_MainTex; };

        void vert (inout appdata_full v)
        {
            UNITY_SETUP_INSTANCE_ID(v);
            float phase = UNITY_ACCESS_INSTANCED_PROP(Props, _Phase);

            float f  = fmod(_Time.y * _Fps * _Speed + phase, _Frames);
            float f0 = floor(f);
            float f1 = fmod(f0 + 1.0, _Frames);
            float t  = f - f0;

            // UV2 = (tile / tiles, (fila + 0.5) / tile_height)
            float tile = floor(v.texcoord1.x * _Tiles + 0.5);
            float y    = v.texcoord1.y;
            float2 uvA = float2((tile * _Frames + f0 + 0.5) / _TexW, y);
            float2 uvB = float2((tile * _Frames + f1 + 0.5) / _TexW, y);

            float3 pA = tex2Dlod(_PosTex, float4(uvA, 0, 0)).xyz;
            float3 pB = tex2Dlod(_PosTex, float4(uvB, 0, 0)).xyz;
            float3 nA = tex2Dlod(_NrmTex, float4(uvA, 0, 0)).xyz;
            float3 nB = tex2Dlod(_NrmTex, float4(uvB, 0, 0)).xyz;

            float3 p = lerp(pA, pB, t);
            v.vertex.xyz = (_OffsetMode > 0.5) ? v.vertex.xyz + p : p;
            v.normal     = normalize(lerp(nA, nB, t));
        }

        void surf (Input IN, inout SurfaceOutputStandard o)
        {
            fixed4 c = tex2D(_MainTex, IN.uv_MainTex) * _Color;
            o.Albedo = c.rgb;
            o.Metallic = _Metallic;
            o.Smoothness = _Glossiness;
            o.Alpha = c.a;
        }
        ENDCG
    }
    FallBack "Diffuse"
}
