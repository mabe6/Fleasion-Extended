"""Common Roblox instance class names used by Fleasion's RBXM preview.

This subset supports the viewer's class picker; unknown binary classes are
still shown, just not suggested by the picker.
"""
ROBLOX_CLASS_NAMES = sorted(set("""
Accessory Accoutrement Animation AnimationController Animator AnimationTrack
Attachment Backpack BackpackItem BillboardGui BodyColors BodyForce BodyGyro
BodyPosition BodyThrust BodyVelocity BoolValue BrickColorValue Camera
CFrameValue Chat ChordSoundEffect ClickDetector Clothing Clouds Color3Value
ColorCorrectionEffect Configuration ControllerManager Decal DepthOfFieldEffect
Dialog DialogChoice Explosion Fire Folder ForceField Frame GuiButton GuiObject
GuiService Humanoid HumanoidDescription ImageButton ImageLabel IntValue JointInstance
LocalScript ManualWeld MaterialVariant MeshPart Message Model ModuleScript Motor6D
NumberValue ObjectValue Pants ParticleEmitter PathfindingModifier PhysicalProperties
PointLight Pose ProximityPrompt RemoteEvent RemoteFunction ReplicatedFirst
ReplicatedStorage ReplicationFocus RopeConstraint ScreenGui Script ScriptContext
Seat SelectionBox ServerScriptService ServerStorage Shirt ShirtGraphic Sky Smoke
Sound SoundGroup Sparkles SpecialMesh SpotLight SpringConstraint StringValue SurfaceAppearance
SurfaceGui SurfaceLight Team TextBox TextButton TextLabel Texture Tool Trail Tween
UIAspectRatioConstraint UIGridLayout UIListLayout UIPadding UIPageLayout UIScale
UISizeConstraint UIStroke UITextSizeConstraint UnionOperation UserInputService ValueBase
Vector3Value VideoFrame ViewportFrame Weld WeldConstraint Workspace WorldModel
WrapLayer WrapTarget CurveAnimation KeyframeSequence Keyframe Pose KeyframeMarker
""".split()))
ROBLOX_CLASS_NAME_SET = set(ROBLOX_CLASS_NAMES)
