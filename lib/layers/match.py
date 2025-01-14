from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import torch
import torch.nn as nn
import torch.nn.functional as F
from layers.lang_encoder import RNNEncoder, PhraseAttention
from layers.visual_encoder import LocationEncoder, SubjectEncoder, RelationEncoder
from layers.lang_decoder import LocationDecoder, SubjectDecoder, RelationDecoder
from layers.loss import AttributeReconstructLoss, LangReconstructionLoss, AdapVisualReconstructLoss, AdapLangReconstructLoss

class Score(nn.Module):
    def __init__(self, vis_dim, lang_dim, jemb_dim):
        super(Score, self).__init__()

        self.feat_fuse = nn.Sequential(nn.Linear(vis_dim+lang_dim, jemb_dim),
                                      nn.ReLU(),
                                      nn.Linear(jemb_dim, 1))
        self.softmax = nn.Softmax(dim=1)
        self.sigmoid = nn.Sigmoid()
        self.lang_dim = lang_dim
        self.vis_dim = vis_dim

    def forward(self, visual_input, lang_input):

        sent_num, ann_num = visual_input.size(0), visual_input.size(1)

        lang_input = lang_input.unsqueeze(1).expand(sent_num, ann_num, self.lang_dim)
        lang_input = nn.functional.normalize(lang_input, p=2, dim=2)

        ann_attn = self.feat_fuse(torch.cat([visual_input, lang_input], 2))

#         ann_attn = self.softmax(ann_attn.view(sent_num, ann_num))
        ann_attn = self.sigmoid(ann_attn.view(sent_num, ann_num))
        ann_attn = ann_attn.unsqueeze(2)

        return ann_attn


class RelationScore(nn.Module):
    def __init__(self, vis_dim, lang_dim, jemb_dim):
        super(RelationScore, self).__init__()

        self.feat_fuse = nn.Sequential(nn.Linear(vis_dim+lang_dim, jemb_dim),
                                      nn.ReLU(),
                                      nn.Linear(jemb_dim, 1))
        self.softmax = nn.Softmax(dim=1)
        self.sigmoid = nn.Sigmoid()
        self.lang_dim = lang_dim
        self.vis_dim = vis_dim

    def forward(self, visual_input, lang_input, masks):

        sent_num, ann_num, cxt_num = visual_input.size(0), visual_input.size(1), visual_input.size(2)

        visual_input = visual_input.view(sent_num, ann_num*cxt_num, -1)
        visual_emb_normalized = nn.functional.normalize(visual_input, p=2, dim=2)
        lang_input = lang_input.unsqueeze(1).expand(sent_num, ann_num, self.lang_dim).contiguous()
        lang_input = lang_input.unsqueeze(2).expand(sent_num, ann_num, cxt_num, self.lang_dim).contiguous()
        lang_input = lang_input.reshape(sent_num, ann_num*cxt_num, -1)
        lang_emb_normalized = nn.functional.normalize(lang_input, p=2, dim=2)


        ann_attn = self.feat_fuse(torch.cat([visual_emb_normalized, lang_emb_normalized], 2))

        ann_attn = ann_attn.squeeze(2).contiguous()
        ann_attn = ann_attn.view(sent_num, ann_num, -1)

        ann_attn = masks * ann_attn
        ann_attn, ixs = torch.max(ann_attn, 2)
#         ann_attn = self.softmax(ann_attn)
        ann_attn = self.sigmoid(ann_attn)
        ann_attn = ann_attn.unsqueeze(2)

        return ann_attn, ixs

class AdaptiveReconstruct(nn.Module):
    def __init__(self, opt):
        super(AdaptiveReconstruct, self).__init__()
        num_layers = opt['rnn_num_layers']
        hidden_size = opt['rnn_hidden_size']
        num_dirs = 2 if opt['bidirectional'] > 0 else 1
        self.word_vec_size = opt['word_vec_size']
        self.pool5_dim, self.fc7_dim = opt['pool5_dim'], opt['fc7_dim']

        self.lang_res_weight = opt['lang_res_weight']
        self.vis_res_weight = opt['vis_res_weight']
        self.att_res_weight = opt['att_res_weight']
        self.loss_combined = opt['loss_combined']
        self.loss_divided = opt['loss_divided']

        # language rnn encoder
        self.rnn_encoder = RNNEncoder(vocab_size=opt['vocab_size'],
                                      word_embedding_size=opt['word_embedding_size'],
                                      word_vec_size=opt['word_vec_size'],
                                      hidden_size=opt['rnn_hidden_size'],
                                      bidirectional=opt['bidirectional']>0,
                                      input_dropout_p=opt['word_drop_out'],
                                      dropout_p=opt['rnn_drop_out'],
                                      n_layers=opt['rnn_num_layers'],
                                      rnn_type=opt['rnn_type'],
                                      variable_lengths=opt['variable_lengths'] > 0)


        self.weight_fc = nn.Linear(num_layers * num_dirs *hidden_size, 3)

        self.sub_attn = PhraseAttention(hidden_size * num_dirs)
        self.loc_attn = PhraseAttention(hidden_size * num_dirs)
        self.rel_attn = PhraseAttention(hidden_size * num_dirs)

        self.sub_encoder = SubjectEncoder(opt)
        self.loc_encoder = LocationEncoder(opt)
        self.rel_encoder = RelationEncoder(opt)

        self.sub_score = Score(self.pool5_dim+self.fc7_dim, opt['word_vec_size'],
                               opt['jemb_dim'])
        self.loc_score = Score(25+5, opt['word_vec_size'],
                               opt['jemb_dim'])
        self.rel_score = RelationScore(self.fc7_dim+5, opt['word_vec_size'],
                               opt['jemb_dim'])

        self.sub_decoder = SubjectDecoder(opt)
        self.loc_decoder = LocationDecoder(opt)
        self.rel_decoder = RelationDecoder(opt)

        self.att_res_loss = AttributeReconstructLoss(opt)
        self.vis_res_loss = AdapVisualReconstructLoss(opt)
        self.lang_res_loss = AdapLangReconstructLoss(opt)
        self.rec_loss = LangReconstructionLoss(opt)

#         self.sub_mlp = nn.Sequential(nn.Linear(opt['jemb_dim'], self.pool5_dim+self.fc7_dim))
#         self.loc_mlp = nn.Sequential(nn.Linear(opt['jemb_dim'], 25+5))
#         self.rel_mlp = nn.Sequential(nn.Linear(opt['jemb_dim'], self.fc7_dim+5))

        self.feat_fuse = nn.Sequential(
            nn.Linear(self.fc7_dim + self.pool5_dim + 25 + 5 + self.fc7_dim + 5, opt['jemb_dim']),
            nn.ReLU())

    def forward(self, pool5, fc7, lfeats, dif_lfeats, cxt_fc7, cxt_lfeats, labels, enc_labels, dec_labels, att_labels, select_ixs, att_weights):

        context, hidden, embedded = self.rnn_encoder(labels)

        weights = F.softmax(self.weight_fc(hidden))
        sub_attn, sub_phrase_emb = self.sub_attn(context, embedded, labels)
        loc_attn, loc_phrase_emb = self.loc_attn(context, embedded, labels)
        rel_attn, rel_phrase_emb = self.rel_attn(context, embedded, labels)

        sent_num = pool5.size(0)
        ann_num = pool5.size(1)

        # subject matching score
        sub_feats = self.sub_encoder(pool5, fc7, sub_phrase_emb)
        sub_ann_attn = self.sub_score(sub_feats, sub_phrase_emb)

        # location matching score
        loc_feats = self.loc_encoder(lfeats, dif_lfeats)
        loc_ann_attn = self.loc_score(loc_feats, loc_phrase_emb)

        # relation matching score
        rel_feats, masks = self.rel_encoder(cxt_fc7, cxt_lfeats)
        rel_ann_attn, rel_ixs = self.rel_score(rel_feats, rel_phrase_emb, masks)

        weights_expand = weights.unsqueeze(1).expand(sent_num, ann_num, 3)
        total_ann_score = (weights_expand * torch.cat([sub_ann_attn, loc_ann_attn, rel_ann_attn], 2)).sum(2)

        loss = 0
        att_res_loss = 0
        lang_res_loss = 0
        vis_res_loss = 0

        # divided_loss
        sub_phrase_recons = self.sub_decoder(sub_feats, total_ann_score)
        loc_phrase_recons = self.loc_decoder(loc_feats, total_ann_score)
        rel_phrase_recons = self.rel_decoder(rel_feats, total_ann_score, rel_ixs)

        if self.vis_res_weight > 0:
            vis_res_loss = self.vis_res_loss(sub_phrase_emb, sub_phrase_recons, loc_phrase_emb,
                                             loc_phrase_recons, rel_phrase_emb, rel_phrase_recons, weights)
            loss = self.vis_res_weight * vis_res_loss
            
        if self.lang_res_weight > 0:
            lang_res_loss = self.lang_res_loss(sub_phrase_emb, loc_phrase_emb, rel_phrase_emb, enc_labels,
                                               dec_labels)

            loss += self.lang_res_weight * lang_res_loss

        # combined_loss
        loss = self.loss_divided*loss
        ann_score = total_ann_score.unsqueeze(1)

        ixs = rel_ixs.view(sent_num, ann_num, 1).unsqueeze(3).expand(sent_num, ann_num, 1, self.fc7_dim + 5)
        rel_feats_max = torch.gather(rel_feats, 2, ixs)
        rel_feats_max = rel_feats_max.squeeze(2)

        fuse_feats = torch.cat([sub_feats, loc_feats, rel_feats_max], 2)
        fuse_feats = torch.bmm(ann_score, fuse_feats)
        fuse_feats = fuse_feats.squeeze(1)
        fuse_feats = self.feat_fuse(fuse_feats)
        rec_loss = self.rec_loss(fuse_feats, enc_labels, dec_labels)
        loss += self.loss_combined * rec_loss
        
        if self.att_res_weight > 0:
            att_scores, att_res_loss = self.att_res_loss(sub_feats, total_ann_score, att_labels, select_ixs, att_weights)
            loss += self.att_res_weight * att_res_loss
 
        # Non-construction loss
        total_ann_score_nocon = (weights_expand * torch.cat([1-sub_ann_attn, 1-loc_ann_attn, 1-rel_ann_attn], 2)).sum(2)

        loss_nocon = 0
        att_res_loss_nocon = 0
        lang_res_loss_nocon = 0
        vis_res_loss_nocon = 0

        # divided_loss
        sub_phrase_recons_nocon = self.sub_decoder(sub_feats, total_ann_score_nocon)
        loc_phrase_recons_nocon = self.loc_decoder(loc_feats, total_ann_score_nocon)
        rel_phrase_recons_nocon = self.rel_decoder(rel_feats, total_ann_score_nocon, rel_ixs)

        if self.vis_res_weight > 0:
            vis_res_loss_nocon = self.vis_res_loss(sub_phrase_emb, sub_phrase_recons_nocon, loc_phrase_emb,
                                             loc_phrase_recons_nocon, rel_phrase_emb, rel_phrase_recons_nocon, weights)
            loss_nocon = self.vis_res_weight * vis_res_loss_nocon

        # combined_loss
        loss_nocon = self.loss_divided*loss_nocon

        ann_score_nocon = total_ann_score_nocon.unsqueeze(1)

        fuse_feats = torch.cat([sub_feats, loc_feats, rel_feats_max], 2)
        fuse_feats_nocon = torch.bmm(ann_score_nocon, fuse_feats)
        fuse_feats_nocon = fuse_feats_nocon.squeeze(1)
        fuse_feats_nocon = self.feat_fuse(fuse_feats_nocon)
        rec_loss_nocon = self.rec_loss(fuse_feats_nocon, enc_labels, dec_labels)
        loss_nocon += self.loss_combined * rec_loss_nocon

        if self.att_res_weight > 0:
            att_scores_nocon, att_res_loss_nocon = self.att_res_loss(sub_feats, total_ann_score_nocon, att_labels, select_ixs, att_weights)
            loss_nocon += self.att_res_weight * att_res_loss_nocon
            
        losses = {}
        losses['loss'] = loss
        losses['vis_res_loss'] = vis_res_loss
        losses['att_res_loss'] = att_res_loss
        losses['lang_res_loss'] = lang_res_loss
        losses['rec_loss'] = rec_loss
        losses['loss_nocon'] = loss_nocon
        losses['vis_res_loss_nocon'] = vis_res_loss_nocon
        losses['att_res_loss_nocon'] = att_res_loss_nocon
        losses['lang_res_loss_nocon'] = lang_res_loss_nocon
        losses['rec_loss_nocon'] = rec_loss_nocon
        return total_ann_score, losses, rel_ixs, sub_attn, loc_attn, rel_attn, weights
    
    def recon_zero_grad(self):
        self.rnn_encoder.zero_grad()
        self.weight_fc.zero_grad()
        self.sub_attn.zero_grad()
        self.loc_attn.zero_grad()
        self.rel_attn.zero_grad()
        self.sub_encoder.zero_grad()
        self.loc_encoder.zero_grad()
        self.rel_encoder.zero_grad()
        self.sub_decoder.zero_grad()
        self.loc_decoder.zero_grad()
        self.rel_decoder.zero_grad()
        self.vis_res_loss.zero_grad()
        self.feat_fuse.zero_grad()
        self.rec_loss.zero_grad()
        self.att_res_loss.zero_grad()
        





