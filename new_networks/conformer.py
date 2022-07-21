import torch
import torch.nn as nn
import torch.nn.functional as F
from functools import partial
from torchsummary import summary
from timm.models.layers import DropPath, trunc_normal_

class Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x


class Attention(nn.Module):
    def __init__(self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0.):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        # NOTE scale factor was wrong in my original version, can set manually to be compat with prev weights
        self.scale = qk_scale or head_dim ** -0.5

        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

    def forward(self, x):
        B, N, C = x.shape
        qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[2]  # make torchscript happy (cannot use tensor as tuple)

        attn = (q @ k.transpose(-2, -1)) * self.scale
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)

        x = (attn @ v).transpose(1, 2).reshape(B, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x,q,k,v


class Block(nn.Module):

    def __init__(self, dim, num_heads, mlp_ratio=4., qkv_bias=False, qk_scale=None, drop=0., attn_drop=0.,
                 drop_path=0., act_layer=nn.GELU, norm_layer=partial(nn.LayerNorm, eps=1e-6)):
        super().__init__()
        self.norm1 = norm_layer(dim)
        self.attn = Attention(
            dim, num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=drop)
        # NOTE: drop path for stochastic depth, we shall see if this is better than dropout here
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)

    def forward(self, x):
        x_norm=self.norm1(x)
        x_att,q,k,v=self.attn(x_norm)
        x = x + self.drop_path(x_att)
        x = x + self.drop_path(self.mlp(self.norm2(x)))
        return x,x_att,q,k,v


class ConvBlock(nn.Module):

    def __init__(self, inplanes, outplanes, stride=1, res_conv=False, act_layer=nn.ReLU, groups=1,
                 norm_layer=partial(nn.BatchNorm2d, eps=1e-6), drop_block=None, drop_path=None):
        super(ConvBlock, self).__init__()

        expansion = 4
        med_planes = outplanes // expansion

        self.conv1 = nn.Conv2d(inplanes, med_planes, kernel_size=1, stride=1, padding=0, bias=False)
        self.bn1 = norm_layer(med_planes)
        self.act1 = act_layer(inplace=True)

        self.conv2 = nn.Conv2d(med_planes, med_planes, kernel_size=3, stride=stride, groups=groups, padding=1, bias=False)
        self.bn2 = norm_layer(med_planes)
        self.act2 = act_layer(inplace=True)

        self.conv3 = nn.Conv2d(med_planes, outplanes, kernel_size=1, stride=1, padding=0, bias=False)
        self.bn3 = norm_layer(outplanes)
        self.act3 = act_layer(inplace=True)

        if res_conv:
            self.residual_conv = nn.Conv2d(inplanes, outplanes, kernel_size=1, stride=stride, padding=0, bias=False)
            self.residual_bn = norm_layer(outplanes)

        self.res_conv = res_conv
        self.drop_block = drop_block
        self.drop_path = drop_path

    def zero_init_last_bn(self):
        nn.init.zeros_(self.bn3.weight)

    def forward(self, x, x_t=None, return_x_2=True):
        residual = x

        x = self.conv1(x)
        x = self.bn1(x)
        if self.drop_block is not None:
            x = self.drop_block(x)
        x = self.act1(x)

        x = self.conv2(x) if x_t is None else self.conv2(x + x_t)
        x = self.bn2(x)
        if self.drop_block is not None:
            x = self.drop_block(x)
        x2 = self.act2(x)

        x = self.conv3(x2)
        x = self.bn3(x)
        if self.drop_block is not None:
            x = self.drop_block(x)

        if self.drop_path is not None:
            x = self.drop_path(x)

        if self.res_conv:
            residual = self.residual_conv(residual)
            residual = self.residual_bn(residual)

        x += residual
        x = self.act3(x)

        if return_x_2:
            return x, x2
        else:
            return x


class FCUDown(nn.Module):
    """ CNN feature maps -> Transformer patch embeddings
    """

    def __init__(self, inplanes, outplanes, dw_stride, act_layer=nn.GELU,
                 norm_layer=partial(nn.LayerNorm, eps=1e-6)):
        super(FCUDown, self).__init__()
        self.dw_stride = dw_stride

        self.conv_project = nn.Conv2d(inplanes, outplanes, kernel_size=1, stride=1, padding=0)
        self.sample_pooling = nn.AvgPool2d(kernel_size=dw_stride, stride=dw_stride)

        self.ln = norm_layer(outplanes)
        self.act = act_layer()

    def forward(self, x, x_t):
        x = self.conv_project(x)  # [N, C, H, W]

        x = self.sample_pooling(x).flatten(2).transpose(1, 2)
        x = self.ln(x)
        x = self.act(x)

        x = torch.cat([x_t[:, 0][:, None, :], x], dim=1)

        return x


class FCUUp(nn.Module):
    """ Transformer patch embeddings -> CNN feature maps
    """

    def __init__(self, inplanes, outplanes, up_stride, act_layer=nn.ReLU,
                 norm_layer=partial(nn.BatchNorm2d, eps=1e-6),):
        super(FCUUp, self).__init__()

        self.up_stride = up_stride
        self.conv_project = nn.Conv2d(inplanes, outplanes, kernel_size=1, stride=1, padding=0)
        self.bn = norm_layer(outplanes)
        self.act = act_layer()

    def forward(self, x, H, W):
        B, _, C = x.shape
        # [N, 197, 384] -> [N, 196, 384] -> [N, 384, 196] -> [N, 384, 14, 14]
        x_r = x[:, 1:].transpose(1, 2).reshape(B, C, H, W)
        x_r = self.act(self.bn(self.conv_project(x_r)))

        return F.interpolate(x_r, size=(H * self.up_stride, W * self.up_stride))


class Med_ConvBlock(nn.Module):
    """ special case for Convblock with down sampling,
    """
    def __init__(self, inplanes, act_layer=nn.ReLU, groups=1, norm_layer=partial(nn.BatchNorm2d, eps=1e-6),
                 drop_block=None, drop_path=None):

        super(Med_ConvBlock, self).__init__()

        expansion = 4
        med_planes = inplanes // expansion

        self.conv1 = nn.Conv2d(inplanes, med_planes, kernel_size=1, stride=1, padding=0, bias=False)
        self.bn1 = norm_layer(med_planes)
        self.act1 = act_layer(inplace=True)

        self.conv2 = nn.Conv2d(med_planes, med_planes, kernel_size=3, stride=1, groups=groups, padding=1, bias=False)
        self.bn2 = norm_layer(med_planes)
        self.act2 = act_layer(inplace=True)

        self.conv3 = nn.Conv2d(med_planes, inplanes, kernel_size=1, stride=1, padding=0, bias=False)
        self.bn3 = norm_layer(inplanes)
        self.act3 = act_layer(inplace=True)

        self.drop_block = drop_block
        self.drop_path = drop_path

    def zero_init_last_bn(self):
        nn.init.zeros_(self.bn3.weight)

    def forward(self, x):
        residual = x

        x = self.conv1(x)
        x = self.bn1(x)
        if self.drop_block is not None:
            x = self.drop_block(x)
        x = self.act1(x)

        x = self.conv2(x)
        x = self.bn2(x)
        if self.drop_block is not None:
            x = self.drop_block(x)
        x = self.act2(x)

        x = self.conv3(x)
        x = self.bn3(x)
        if self.drop_block is not None:
            x = self.drop_block(x)

        if self.drop_path is not None:
            x = self.drop_path(x)

        x += residual
        x = self.act3(x)

        return x


class ConvTransBlock(nn.Module):
    """
    Basic module for ConvTransformer, keep feature maps for CNN block and patch embeddings for transformer encoder block
    """

    def __init__(self, inplanes, outplanes, res_conv, stride, dw_stride, embed_dim, num_heads=12, mlp_ratio=4.,
                 qkv_bias=False, qk_scale=None, drop_rate=0., attn_drop_rate=0., drop_path_rate=0.,
                 last_fusion=False, num_med_block=0, groups=1):

        super(ConvTransBlock, self).__init__()
        expansion = 4
        self.cnn_block = ConvBlock(inplanes=inplanes, outplanes=outplanes, res_conv=res_conv, stride=stride, groups=groups)

        if last_fusion:
            self.fusion_block = ConvBlock(inplanes=outplanes, outplanes=outplanes, stride=2, res_conv=True, groups=groups)
        else:
            self.fusion_block = ConvBlock(inplanes=outplanes, outplanes=outplanes, groups=groups)

        if num_med_block > 0:
            self.med_block = []
            for i in range(num_med_block):
                self.med_block.append(Med_ConvBlock(inplanes=outplanes, groups=groups))
            self.med_block = nn.ModuleList(self.med_block)

        self.squeeze_block = FCUDown(inplanes=outplanes // expansion, outplanes=embed_dim, dw_stride=dw_stride)

        self.expand_block = FCUUp(inplanes=embed_dim, outplanes=outplanes // expansion, up_stride=dw_stride)

        self.trans_block = Block(
            dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
            drop=drop_rate, attn_drop=attn_drop_rate, drop_path=drop_path_rate)

        self.dw_stride = dw_stride
        self.embed_dim = embed_dim
        self.num_med_block = num_med_block
        self.last_fusion = last_fusion

    def forward(self, x, x_t):
        x, x2 = self.cnn_block(x)
        #print('cnn_block',x.shape,x2.shape)
        _, _, H, W = x2.shape

        x_st = self.squeeze_block(x2, x_t)
        #print('FCdown_block',x_st.shape)
        x_t,x_att,q,k,v = self.trans_block(x_st + x_t)
        #print('tran_block',x_t.shape,q.shape)
        if self.num_med_block > 0:
            for m in self.med_block:
                x = m(x)

        x_t_r = self.expand_block(x_t, H // self.dw_stride, W // self.dw_stride)
        #print('FCUP_block',x_t_r.shape)
        x = self.fusion_block(x, x_t_r, return_x_2=False)
        #print('fusion_block',x.shape)

        return x,x_att, x_t,q,k,v


class Conformer(nn.Module):

    def __init__(self, patch_size=16, in_chans=3, num_classes=1000, base_channel=64, channel_ratio=4, num_med_block=0,
                 embed_dim=768, depth=12, num_heads=12, mlp_ratio=4., qkv_bias=False, qk_scale=None,
                 drop_rate=0., attn_drop_rate=0., drop_path_rate=0.):

        # Transformer
        super().__init__()
        self.num_classes = num_classes
        self.num_features = self.embed_dim = embed_dim  # num_features for consistency with other models
        assert depth % 3 == 0

        self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))
        self.trans_dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule

        # Classifier head
        self.trans_norm = nn.LayerNorm(embed_dim)
        self.trans_cls_head = nn.Linear(embed_dim, num_classes) if num_classes > 0 else nn.Identity()
        self.pooling = nn.AdaptiveAvgPool2d(1)
        self.conv_cls_head = nn.Linear(int(256 * channel_ratio), num_classes)

        # Stem stage: get the feature maps by conv block (copied form resnet.py)
        self.conv1 = nn.Conv2d(in_chans, 64, kernel_size=7, stride=2, padding=3, bias=False)  # 1 / 2 [112, 112]
        self.bn1 = nn.BatchNorm2d(64)
        self.act1 = nn.ReLU(inplace=True)
        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)  # 1 / 4 [56, 56]

        # 1 stage
        stage_1_channel = int(base_channel * channel_ratio)
        trans_dw_stride = patch_size // 4
        self.conv_1 = ConvBlock(inplanes=64, outplanes=stage_1_channel, res_conv=True, stride=1)
        self.trans_patch_conv = nn.Conv2d(64, embed_dim, kernel_size=trans_dw_stride, stride=trans_dw_stride, padding=0)
        self.trans_1 = Block(dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias,
                             qk_scale=qk_scale, drop=drop_rate, attn_drop=attn_drop_rate, drop_path=self.trans_dpr[0],
                             )

        # 2~4 stage
        init_stage = 2
        fin_stage = depth // 3 + 1
        for i in range(init_stage, fin_stage):
            self.add_module('conv_trans_' + str(i),
                    ConvTransBlock(
                        stage_1_channel, stage_1_channel, False, 1, dw_stride=trans_dw_stride, embed_dim=embed_dim,
                        num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                        drop_rate=drop_rate, attn_drop_rate=attn_drop_rate, drop_path_rate=self.trans_dpr[i-1],
                        num_med_block=num_med_block
                    )
            )


        stage_2_channel = int(base_channel * channel_ratio * 2)
        # 5~8 stage
        init_stage = fin_stage # 5
        fin_stage = fin_stage + depth // 3 # 9
        for i in range(init_stage, fin_stage):
            s = 2 if i == init_stage else 1
            in_channel = stage_1_channel if i == init_stage else stage_2_channel
            res_conv = True if i == init_stage else False
            self.add_module('conv_trans_' + str(i),
                    ConvTransBlock(
                        in_channel, stage_2_channel, res_conv, s, dw_stride=trans_dw_stride // 2, embed_dim=embed_dim,
                        num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                        drop_rate=drop_rate, attn_drop_rate=attn_drop_rate, drop_path_rate=self.trans_dpr[i-1],
                        num_med_block=num_med_block
                    )
            )

        stage_3_channel = int(base_channel * channel_ratio * 2 * 2)
        # 9~12 stage
        init_stage = fin_stage  # 9
        fin_stage = fin_stage + depth // 3  # 13
        for i in range(init_stage, fin_stage):
            s = 2 if i == init_stage else 1
            in_channel = stage_2_channel if i == init_stage else stage_3_channel
            res_conv = True if i == init_stage else False
            last_fusion = True if i == depth else False
            self.add_module('conv_trans_' + str(i),
                    ConvTransBlock(
                        in_channel, stage_3_channel, res_conv, s, dw_stride=trans_dw_stride // 4, embed_dim=embed_dim,
                        num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                        drop_rate=drop_rate, attn_drop_rate=attn_drop_rate, drop_path_rate=self.trans_dpr[i-1],
                        num_med_block=num_med_block, last_fusion=last_fusion
                    )
            )
        self.fin_stage = fin_stage

        trunc_normal_(self.cls_token, std=.02)

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.Conv2d):
            nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
        elif isinstance(m, nn.BatchNorm2d):
            nn.init.constant_(m.weight, 1.)
            nn.init.constant_(m.bias, 0.)
        elif isinstance(m, nn.GroupNorm):
            nn.init.constant_(m.weight, 1.)
            nn.init.constant_(m.bias, 0.)

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'cls_token'}


    def forward(self, x,y):
        #B = x.shape[0]
        B = y.shape[0]
        cls_tokens = self.cls_token.expand(B, -1, -1)
        conv_features=[]
        tran_features=[]
        q=[]
        k=[]
        v=[]
        x_att=[]
        # pdb.set_trace()
        # stem stage [N, 3, 224, 224] -> [N, 64, 56, 56]
        x_base = self.maxpool(self.act1(self.bn1(self.conv1(x))))
        y_base = self.maxpool(self.act1(self.bn1(self.conv1(y))))
        #print('x_base',x_base.shape)
        conv_features.append(x_base)
        tran_features.append(y_base)
        # 1 stage
        x = self.conv_1(x_base, return_x_2=False)
        conv_features.append(x)

        y_t = self.trans_patch_conv(y_base).flatten(2).transpose(1, 2)
        #print('x_t flatten',x_t.shape)
        tran_features.append(y_t)
       
        y_t = torch.cat([cls_tokens, y_t], dim=1)
        #print('y_t n tokens',y_t.shape)
        y_t,x_att1,q1,k1,v1 = self.trans_1(y_t)
        #print('y_t tran_1 q k  v',y_t.shape,q1.shape,k1.shape,v1.shape)
        tran_features.append(y_t)
        q.append(q1)
        k.append(k1)
        v.append(v1)
        x_att.append(x_att1)
        # 2 ~ final 
        for i in range(2, self.fin_stage):
            x, x_atti,y_t,qi,ki,vi = eval('self.conv_trans_' + str(i))(x, y_t)
            conv_features.append(x)
            tran_features.append(y_t)
            q.append(qi)
            k.append(ki)
            v.append(vi)
            x_att.append(x_atti)
        
        return conv_features,tran_features,q,k,v,x_att

class JLModule(nn.Module):
    def __init__(self, backbone):
        super(JLModule, self).__init__()
        self.backbone = backbone
        

    def load_pretrained_model(self, model_path):
        pretrained_dict = torch.load(model_path)
        model_dict = self.backbone.state_dict()
        pretrained_dict = {ka: va for ka, va in pretrained_dict.items() if ka in model_dict}
        model_dict.update(pretrained_dict)
        self.backbone.load_state_dict(model_dict)
        

    def forward(self, x,y):

        conv,tran,q,k,v,x_att = self.backbone(x,y)
        '''for i in range(len(conv)):
            print(i,"     ",conv[i].shape,tran[i].shape)'''
        

        return conv,tran,q,k,v,x_att # list of tensor that compress model output

class LDELayer(nn.Module):
    def __init__(self):
        super(LDELayer, self).__init__()
        self.relu = nn.ReLU()
        self.conv_c=nn.Sequential(nn.Conv2d(256,384,1,1),self.relu)
        self.conv_rgb=nn.Sequential(nn.MaxPool2d(3),nn.Conv2d(256, 64, 7, 1, 1), nn.Conv2d(64, 64, 5, 1, 1), self.relu)
        self.pool_avg = nn.AvgPool2d(kernel_size=4, stride=4)
        self.softmax=nn.Softmax(dim=1)

    def forward(self, list_x,list_y,q,k,v):
        #fconv_c=[]
        #fconv_d=[]
        #rgb_lde=[]
        #depth_lde=[]
        
        '''for i in range(len(list_x)):
            rgb_conv = list_x[i][0]
            depth_conv = list_x[i][1]
            rgb_tran = list_y[i][0]
            depth_tran = list_y[i][1]
            print("******LDE layer******")
            print(i,"     ",rgb_conv.shape,rgb_tran.shape,depth_tran.shape)'''
        j=4
        fconv_c=self.conv_c(list_x[j])
        #print('fconv_c',fconv_c.shape)
        fconv_c=self.pool_avg(fconv_c)
        #print('fconv_c',fconv_c.shape)
        fconv_c=fconv_c.flatten(2).transpose(1,2)
        #print('fconv_c',fconv_c.shape)
        fconv_c=torch.cat([list_y[j][:, 0][:, None, :], fconv_c], dim=1)
        #print('fconv_c',fconv_c.shape)
        q[j]=q[j].permute(0,2,1,3).flatten(2)
        k[j]=k[j].permute(0,2,1,3).flatten(2)
        v[j]=v[j].permute(0,2,1,3).flatten(2)
        depth_lde=fconv_c*(self.softmax((q[j]*list_y[j])*k[j])*v[j])
        rgb_lde=self.conv_rgb(list_x[j])
             
        print('rgb_lde',rgb_lde.shape,depth_lde.shape)


        return rgb_lde,depth_lde

class CoarseLayer(nn.Module):
    def __init__(self):
        super(CoarseLayer, self).__init__()
        self.conv_r = nn.Conv2d(1024, 1, 1, 1)
        self.conv_d=nn.Conv2d(384,1,3,2,1)
        

    def forward(self, x, y):
        #print('corase',x.shape,y.shape)
        B, _, C = y.shape
        _,_,H,W=x.shape
        y_r = y[:, 1:].transpose(1, 2).unflatten(2,(H*2,W*2))
        #print('after corase',x.shape,y_r.shape)
        sal_rgb=self.conv_r(x)
        sal_depth=self.conv_d(y_r)
        #print('sal r and ',sal_rgb.shape,sal_depth.shape)
        return sal_rgb,sal_depth

class GDELayer(nn.Module):
    def __init__(self):
        super(GDELayer, self).__init__()
        k=1
        self.sigmoid = nn.Sigmoid()
        self.conv1024=nn.Conv2d(1024,1,1,1)
        self.conv512=nn.Conv2d(512,1,1,1)
        self.conv384=nn.Conv2d(384,1,1,1)
        self.upsampling= nn.ConvTranspose2d(k,k, kernel_size=4, stride=2 , padding=1) # 10x10 to 20x20
        self.upsampling11= nn.ConvTranspose2d(k,k, kernel_size=4, stride=4 , padding=0)# 10x10 to 40x40
        self.upsampling22= nn.ConvTranspose2d(384,k, kernel_size=4, stride=2 , padding=1) 
        

    def forward(self, x, y,coarse_sal_rgb,coarse_sal_depth):
        
        rgb_h=torch.zeros(coarse_sal_rgb.size(0),1,20,20).cuda()
        rgb_m=torch.zeros(coarse_sal_rgb.size(0),1,40,40).cuda()
        depth_h=torch.zeros(coarse_sal_rgb.size(0),1,20,20).cuda()
        depth_m=torch.zeros(coarse_sal_rgb.size(0),1,40,40).cuda()
        for j in range(11,7,-3):
            rgb_part=x[j]
            depth_part=y[j]
            B, _, C = depth_part.shape
            Br,Cr,Hr,Wr=x[j].shape
            # [N, 197, 384] -> [N, 196, 384] -> [N, 384, 196] -> [N, 384, 14, 14]

            #x_r = self.act(self.bn(self.conv_project(x_r)))
            #print(j,rgb_part.shape)
            if (rgb_part.size(1)==1024):
                rgb_part=self.conv1024(rgb_part)
                coarse_sal_rgb1=self.upsampling(coarse_sal_rgb)
                coarse_sal_depth1=self.upsampling(coarse_sal_depth)
                y_r = depth_part[:, 1:].transpose(1, 2).unflatten(2,(20,20))
                y_r=self.conv384(y_r)

                salr=self.sigmoid(coarse_sal_rgb1)
                Ar=1-salr
                rgb_h+=Ar*rgb_part

                sald=self.sigmoid(coarse_sal_depth1)
                Ad=1-sald
                depth_h+=Ad*y_r
            


            else:
                rgb_part=self.conv512(rgb_part)
                coarse_sal_rgb1=self.upsampling11(coarse_sal_rgb)
                coarse_sal_depth1=self.upsampling11(coarse_sal_depth)
                y_r = depth_part[:, 1:].transpose(1, 2).unflatten(2,(20,20))
                y_r=self.upsampling22(y_r)

                salr=self.sigmoid(coarse_sal_rgb1)
                Ar=1-salr
                rgb_m+=Ar*rgb_part

                sald=self.sigmoid(coarse_sal_depth1)
                Ad=1-sald
                depth_m+=Ad*y_r
            
            
        #print('gde',rgb_h.shape,rgb_m.shape,depth_h.shape,depth_m.shape)     
        return rgb_h,rgb_m,depth_h,depth_m

class Decoder(nn.Module):
    def __init__(self):
        super(Decoder, self).__init__()
        self.upsample=nn.ConvTranspose2d(64, 1, kernel_size=3, stride=4, padding=1, output_padding=3,dilation=1)
        self.upsample1=nn.ConvTranspose2d(384, 1, kernel_size=3, stride=4, padding=1, output_padding=3,dilation=1)
        self.up2= nn.ConvTranspose2d(1, 1, kernel_size=4, stride=2, padding=1)     
        
    def forward(self, lde_c,lde_t,rgb_h,rgb_m,depth_h,depth_m):
        sal_high=rgb_h+depth_h
        sal_med=rgb_m+depth_m
        rgb_l=self.upsample(lde_c)
        d=lde_t[:, 1:].transpose(1, 2).unflatten(2,(20,20))
        depth_l=self.upsample1(lde_t[:, 1:].transpose(1, 2).unflatten(2,(20,20)))
        sal_low=rgb_l+depth_l
        sal_final=self.up2(self.up2(sal_low+self.up2((sal_med+(self.up2(sal_high))))))
        #print(sal_high.shape,sal_med.shape,sal_low.shape, sal_final.shape)

        return sal_final,sal_low,sal_med,sal_high


class JL_DCF(nn.Module):
    def __init__(self,JLModule,lde_layers,coarse_layer,gde_layers,decoder):
        super(JL_DCF, self).__init__()
        
        self.JLModule = JLModule
        self.lde = lde_layers
        self.coarse_layer=coarse_layer
        self.gde_layers=gde_layers
        self.decoder=decoder
        self.final_conv=nn.Conv2d(8,1,1,1,0)
        
    def forward(self, f_all,f1_all):
        x,y,q,k,v,Att = self.JLModule(f_all,f1_all)
        lde_c,lde_t = self.lde(x,y,q,k,v)
        coarse_sal_rgb,coarse_sal_depth=self.coarse_layer(x[12],y[12])
        rgb_h,rgb_m,depth_h,depth_m=self.gde_layers(x,y,coarse_sal_rgb,coarse_sal_depth)

        sal_final,sal_low,sal_med,sal_high=self.decoder(lde_c,lde_t,rgb_h,rgb_m,depth_h,depth_m)

        return sal_final,sal_low,sal_med,sal_high,coarse_sal_rgb,coarse_sal_depth,Att

def build_model(network='conformer', base_model_cfg='conformer'):
   
        backbone= Conformer(patch_size=16, channel_ratio=4, embed_dim=384, depth=12,
                      num_heads=6, mlp_ratio=4, qkv_bias=True)
        
   

        return JL_DCF(JLModule(backbone),LDELayer(),CoarseLayer(),GDELayer(),Decoder())
