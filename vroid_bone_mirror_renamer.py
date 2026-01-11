bl_info = {
    "name": "VRoid/VRM Bone Mirror Renamer",
    "author": "ChatGPT",
    "version": (1, 0, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar (N) > VRoid",
    "description": "Rename VRoid/VRM bones to Blender mirror-friendly .L/.R suffixes",
    "category": "Rigging",
}

import bpy
import re
from bpy.props import BoolProperty, EnumProperty, FloatProperty


# -----------------------------
# Helpers
# -----------------------------
SIDE_SUFFIX_PATTERNS = [
    (re.compile(r"\.L$"), "L"),
    (re.compile(r"\.R$"), "R"),
    (re.compile(r"\.l$"), "L"),
    (re.compile(r"\.r$"), "R"),
    (re.compile(r"_L$"), "L"),
    (re.compile(r"_R$"), "R"),
    (re.compile(r"-L$"), "L"),
    (re.compile(r"-R$"), "R"),
    (re.compile(r" Left$"), "L"),
    (re.compile(r" Right$"), "R"),
    (re.compile(r"_Left$"), "L"),
    (re.compile(r"_Right$"), "R"),
    (re.compile(r"左$"), "L"),
    (re.compile(r"右$"), "R"),
]

MID_LR_PATTERNS = [
    # typical VRoid/VRM style: J_Bip_L_UpperArm / J_Bip_R_UpperArm
    (re.compile(r"(.*)_L_(.*)"), "L"),
    (re.compile(r"(.*)_R_(.*)"), "R"),
    # sometimes: xxx.Left.xxx, xxx.Right.xxx
    (re.compile(r"(.*)\.Left\.(.*)"), "L"),
    (re.compile(r"(.*)\.Right\.(.*)"), "R"),
    (re.compile(r"(.*)_Left_(.*)"), "L"),
    (re.compile(r"(.*)_Right_(.*)"), "R"),
    # Japanese in middle
    (re.compile(r"(.*)左(.*)"), "L"),
    (re.compile(r"(.*)右(.*)"), "R"),
]


def get_suffix_style(style_key: str, side: str) -> str:
    # side: "L" or "R"
    if style_key == "DOT":
        return f".{side}"
    if style_key == "UNDERSCORE":
        return f"_{side}"
    if style_key == "DASH":
        return f"-{side}"
    # fallback
    return f".{side}"


def strip_existing_side_suffix(name: str) -> str:
    for rx, _side in SIDE_SUFFIX_PATTERNS:
        if rx.search(name):
            return rx.sub("", name)
    return name


def detect_side_from_name(name: str):
    # returns ("L"/"R"/None, base_name)
    for rx, side in SIDE_SUFFIX_PATTERNS:
        if rx.search(name):
            base = rx.sub("", name)
            return side, base

    # detect mid patterns and rebuild base
    for rx, side in MID_LR_PATTERNS:
        m = rx.fullmatch(name)
        if m:
            base = (m.group(1) + "_" + m.group(2)).strip("_")
            return side, base

    # also: explicit tokens
    lowered = name.lower()
    if "left" in lowered and "right" not in lowered:
        base = re.sub("(?i)left", "", name).replace("__", "_").strip("_ .-")
        return "L", base
    if "right" in lowered and "left" not in lowered:
        base = re.sub("(?i)right", "", name).replace("__", "_").strip("_ .-")
        return "R", base

    return None, name


def bone_center_x_in_armature_space(bone):
    # bone.head_local / tail_local are in armature space
    x = (bone.head_local.x + bone.tail_local.x) * 0.5
    return x


def make_unique_name(target_names_set, desired):
    if desired not in target_names_set:
        target_names_set.add(desired)
        return desired
    # add numeric suffix
    i = 1
    while True:
        candidate = f"{desired}.{i:03d}"
        if candidate not in target_names_set:
            target_names_set.add(candidate)
            return candidate
        i += 1


# -----------------------------
# Operator
# -----------------------------
class VROID_OT_bone_mirror_renamer(bpy.types.Operator):
    bl_idname = "vroid.bone_mirror_renamer"
    bl_label = "Rename bones for Mirror (.L/.R)"
    bl_options = {"REGISTER", "UNDO"}

    only_selected: BoolProperty(
        name="Only selected bones",
        default=False,
        description="If enabled, rename only selected bones (Pose Mode selection)",
    )

    convert_mid_lr: BoolProperty(
        name="Convert mid-name L/R (e.g., _L_ / _R_)",
        default=True,
        description="Convert names like J_Bip_L_UpperArm to J_Bip_UpperArm.L",
    )

    use_position_fallback: BoolProperty(
        name="Use X-position fallback for side detection",
        default=True,
        description="If a bone name doesn't include side, decide L/R by bone X position",
    )

    center_threshold: FloatProperty(
        name="Center threshold",
        default=0.001,
        min=0.0,
        max=1.0,
        description="If |X| is below this threshold, treat as center (no L/R suffix)",
    )

    suffix_style: EnumProperty(
        name="Suffix style",
        items=[
            ("DOT", ".L / .R (recommended)", ""),
            ("UNDERSCORE", "_L / _R", ""),
            ("DASH", "-L / -R", ""),
        ],
        default="DOT",
    )

    keep_existing_suffix: BoolProperty(
        name="Keep already-correct suffix",
        default=True,
        description="If a bone already ends with a recognized L/R suffix, keep it",
    )

    def execute(self, context):
        obj = context.object
        if not obj or obj.type != "ARMATURE":
            self.report({"ERROR"}, "Select an Armature object.")
            return {"CANCELLED"}

        arm = obj.data

        # Determine target bones
        bones = []
        if self.only_selected and obj.mode == "POSE":
            bones = [pb.bone for pb in context.selected_pose_bones] if context.selected_pose_bones else []
        else:
            bones = list(arm.bones)

        if not bones:
            self.report({"WARNING"}, "No target bones found (select bones in Pose Mode, or disable 'Only selected').")
            return {"CANCELLED"}

        # Build existing names set to keep uniqueness
        existing_names = set(b.name for b in arm.bones)

        renamed = 0
        skipped = 0

        # We rename armature bones; must be in Object or Pose mode is fine (Blender allows editing bone names)
        for b in bones:
            old = b.name

            # If keeping existing suffix and it already ends with recognizable suffix, skip
            if self.keep_existing_suffix:
                already_side, base = detect_side_from_name(old)
                # If it detected side via *end suffix* (not mid), we consider it "already correct"
                is_end_suffix = any(rx.search(old) for rx, _ in SIDE_SUFFIX_PATTERNS)
                if already_side in ("L", "R") and is_end_suffix:
                    skipped += 1
                    continue

            side, base = detect_side_from_name(old)

            # If user disabled mid conversion, ignore mid-only detections
            if not self.convert_mid_lr:
                # re-detect only end suffix
                side2 = None
                base2 = old
                for rx, s in SIDE_SUFFIX_PATTERNS:
                    if rx.search(old):
                        side2 = s
                        base2 = rx.sub("", old)
                        break
                side, base = side2, base2

            # If no side found and fallback enabled, use position
            if side is None and self.use_position_fallback:
                x = bone_center_x_in_armature_space(b)
                if abs(x) <= self.center_threshold:
                    # center bone: remove side suffix if any weirdness, keep base
                    side = None
                else:
                    side = "L" if x < 0.0 else "R"
                    base = strip_existing_side_suffix(base)

            # If still no side, normalize name a bit and continue
            if side is None:
                # Optionally strip weird trailing markers, but keep original if safe
                skipped += 1
                continue

            base = base.strip(" _.-")
            suffix = get_suffix_style(self.suffix_style, side)
            desired = f"{base}{suffix}"

            if desired == old:
                skipped += 1
                continue

            # Ensure unique
            existing_names.discard(old)
            final = make_unique_name(existing_names, desired)

            b.name = final
            renamed += 1

        self.report({"INFO"}, f"Renamed: {renamed}, Skipped: {skipped}")
        return {"FINISHED"}


# -----------------------------
# UI Panel
# -----------------------------
class VROID_PT_bone_mirror_renamer_panel(bpy.types.Panel):
    bl_label = "VRoid Bone Mirror Renamer"
    bl_idname = "VROID_PT_bone_mirror_renamer_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "VRoid"

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)

        op = col.operator(VROID_OT_bone_mirror_renamer.bl_idname, icon="ARMATURE_DATA")

        col.separator()
        col.label(text="Options")
        col.prop(context.scene, "vroid_ren_only_selected")
        col.prop(context.scene, "vroid_ren_convert_mid_lr")
        col.prop(context.scene, "vroid_ren_use_position_fallback")
        col.prop(context.scene, "vroid_ren_center_threshold")
        col.prop(context.scene, "vroid_ren_suffix_style")
        col.prop(context.scene, "vroid_ren_keep_existing_suffix")

        # Bind scene props -> operator defaults (so UI changes apply)
        op.only_selected = context.scene.vroid_ren_only_selected
        op.convert_mid_lr = context.scene.vroid_ren_convert_mid_lr
        op.use_position_fallback = context.scene.vroid_ren_use_position_fallback
        op.center_threshold = context.scene.vroid_ren_center_threshold
        op.suffix_style = context.scene.vroid_ren_suffix_style
        op.keep_existing_suffix = context.scene.vroid_ren_keep_existing_suffix


# -----------------------------
# Registration + Scene props
# -----------------------------
classes = (
    VROID_OT_bone_mirror_renamer,
    VROID_PT_bone_mirror_renamer_panel,
)

def register():
    for c in classes:
        bpy.utils.register_class(c)

    bpy.types.Scene.vroid_ren_only_selected = BoolProperty(
        name="Only selected bones",
        default=False,
    )
    bpy.types.Scene.vroid_ren_convert_mid_lr = BoolProperty(
        name="Convert mid-name L/R",
        default=True,
    )
    bpy.types.Scene.vroid_ren_use_position_fallback = BoolProperty(
        name="Use X-position fallback",
        default=True,
    )
    bpy.types.Scene.vroid_ren_center_threshold = FloatProperty(
        name="Center threshold",
        default=0.001,
        min=0.0,
        max=1.0,
    )
    bpy.types.Scene.vroid_ren_suffix_style = EnumProperty(
        name="Suffix style",
        items=[
            ("DOT", ".L / .R (recommended)", ""),
            ("UNDERSCORE", "_L / _R", ""),
            ("DASH", "-L / -R", ""),
        ],
        default="DOT",
    )
    bpy.types.Scene.vroid_ren_keep_existing_suffix = BoolProperty(
        name="Keep already-correct suffix",
        default=True,
    )

def unregister():
    del bpy.types.Scene.vroid_ren_only_selected
    del bpy.types.Scene.vroid_ren_convert_mid_lr
    del bpy.types.Scene.vroid_ren_use_position_fallback
    del bpy.types.Scene.vroid_ren_center_threshold
    del bpy.types.Scene.vroid_ren_suffix_style
    del bpy.types.Scene.vroid_ren_keep_existing_suffix

    for c in reversed(classes):
        bpy.utils.unregister_class(c)

if __name__ == "__main__":
    register()
